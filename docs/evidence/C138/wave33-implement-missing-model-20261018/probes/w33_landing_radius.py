"""Wave-33 landing radius and attribution: the re-sign price, the movers, and gates that still bite.

Shape and discipline copied from ``docs/evidence/C137/wave32-yfinance-landing-20261018/probes/
w32_landing_radius.py``: the shipped judge is imported in-process, every reading comes from this
run, and no pinned artifact is written (the judge writes only under /tmp).

What wave 33 did: 15 new domains in ``opendata/data/domains.yaml`` (338 -> 353 keys) with fetchers
for tmx (4 GraphQL operations, ``_OPERATIONS`` 7 -> 11; ETF loaders) and
``government_us::TreasuryPrices``, witness shards under ``iter2_witnesses/_parts/``, and a
28-provider re-sign chain. Plane 1 moves 317 -> 332 of 350.

The arms this face has to be able to fail on, each numbered in the print stream:

* A -- the pre-chain state: judge HEAD's ``claims.merged.json`` against today's bytes. The price
  of the re-sign, measured: wave 32 read 291 STALE because the runner is pinned by every receipt;
  wave 33 reads 317 STALE because ``domains.yaml`` is pinned by every accepted row (1585 slots).
* B/C -- post-chain 332/350 with the full status dict, and the shipped staleness walk (HEAD-side
  stale=317, disk stale=0, a forged digest still stale=332: the detector bites).
* D/E/F -- attribution. D is the *trap* wave 32's face section 5 documented: a recomputed HEAD
  reads 0 accepted, so HEAD-vs-disk shows 28 movers. E compares against the report committed at
  HEAD instead and must show movers == {tmx +14, government_us +1} with zero regressions; F
  forges one accepted row in memory to prove E can go red, then rechecks.
* G -- the census flip residue 33 -> 18 with the shipped action table, plus what the census's own
  arms_failed reading currently is and why (wave 32 consumed its negative control).
* H -- the GraphQL allow-list grew 7 -> 11, so the face must show the gate still bites: a live
  ``TMX_OPERATION_NOT_ALLOWED`` for an operation that is NOT registered.
* I -- refusal shape (wave 32 lesson: an AST verdict is not reachability): the shipped
  ``--phase counterfact`` leg re-run for one GraphQL-family row and the government_us row, against
  a scratch evidence root, printing the verbatim refusal and the site the reason names.
* J -- instrument finding, measured both sides: the witness seam does NOT enforce the declared
  ``allowed_hosts`` on offline legs (a forged host list still serves rows), while the real request
  path admits only hosts inside the grant (request_budget.py).
* K -- carriers: walk ``runs/*.txt`` minus this probe's own carrier, name the population, every
  chain reading zero, merge idempotent, recorders' signed/refused counts.

Child modes (this file re-enters itself): ``--child bogushosts <provider> <model>`` runs one
offline direct leg with a forged host grant in-process.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess  # nosec B404 B604  # read-only evidence probe; every argv is a literal list
import sys
from pathlib import Path
from typing import Any, Final

REPO_ROOT: Final = Path(__file__).resolve().parents[5]
BUNDLE: Final = "docs/evidence/C138/wave33-implement-missing-model-20261018"
C101: Final = "docs/evidence/C101"
DOMAINS: Final = "opendata/data/domains.yaml"
INVENTORY: Final = "docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/模型级任务清单.csv"
LANDED_REPORT: Final = f"{C101}/judge/claims.merged.report.json"
C135_PIN_WALK: Final = (
    "docs/evidence/C135/wave31-step6-domains-pin-blast-radius-20261018/"
    "probes/domains_pin_blast_radius.py"
)
CENSUS: Final = "docs/evidence/C128/plane1-residue-readiness-20261017/probes/residue_axis_census.py"
RUNNER: Final = "scripts/quality/iter2_claims_probe_runner.py"
BUDGET: Final = "opendata/data/request_budget.py"
TMX_CLIENT: Final = "opendata/data/providers/tmx/models/client.py"
SCRATCH: Final = Path("/tmp/w33_landing_radius")  # noqa: S108  # nosec B108  # judge --output only
LEGS_EVIDENCE_ROOT: Final = SCRATCH / "evidence_root"
LEG_ROWS: Final = (("tmx", "EtfHistorical"), ("government_us", "TreasuryPrices"))
PIN_GROUPS: Final = ("contract", "fixture", "probe", "source")
ACCEPTED: Final = "OFFLINE_ACCEPTED"
MISSED: Final = "UNREVIEWED"
SELF_CARRIER: Final = Path(__file__).with_suffix(".txt").name
CARRIER_EXIT_LINE: Final = re.compile(r"\b\w*_(?:exit|rc)\s*=\s*-?\d+")


def _run(argv: list[str], *, env: dict[str, str] | None = None) -> tuple[int, str]:
    proc = subprocess.run(  # noqa: S603  # nosec B603  # literal argv, no shell interpolation
        argv, capture_output=True, text=True, cwd=REPO_ROOT, check=False, timeout=900, env=env
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _git_show(relative: str, rev: str = "HEAD") -> str:
    rc, out = _run(["git", "show", f"{rev}:{relative}"])  # nosec B607
    if rc != 0:
        raise RuntimeError(f"git show {rev}:{relative} failed rc={rc}")
    return out


def _judge(claims_rel: str, out_name: str) -> dict[str, Any]:
    sys.path.insert(0, str(REPO_ROOT))
    from scripts.quality import iter2_model_evidence as judge

    SCRATCH.mkdir(parents=True, exist_ok=True)
    target = SCRATCH / out_name
    rc = judge.main(["--inventory", INVENTORY, "--claims", claims_rel, "--output", str(target)])
    report = json.loads(target.read_text(encoding="utf-8"))
    statuses: dict[str, int] = {}
    per_provider: dict[str, int] = {}
    for model in report["models"]:
        statuses[model["status"]] = statuses.get(model["status"], 0) + 1
        if model["status"] == ACCEPTED:
            per_provider[model["provider"]] = per_provider.get(model["provider"], 0) + 1
    return {
        "judge_exit": rc,
        "identity_set_sha256": report["inventory"]["identity_set_sha256"][:16],
        "identity_count": report["summary"]["identity_count"],
        "claim_count": report["summary"]["claim_count"],
        "accepted": report["summary"]["offline_acceptance_count"],
        "statuses": statuses,
        "accepted_by_provider": per_provider,
    }


def _pin_census(claims: list[dict[str, Any]], rel: str) -> tuple[int, int]:
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


def _pin_walk(claims: list[dict[str, Any]], rel: str, digest: str | None) -> dict[str, Any]:
    import importlib.util

    spec = importlib.util.spec_from_file_location("pinwalk", str(REPO_ROOT / C135_PIN_WALK))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {C135_PIN_WALK}")
    loaded: Any = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(REPO_ROOT))
    spec.loader.exec_module(loaded)
    rows, slots, issues, seen = loaded._walk_claims(claims, rel, digest)
    return {
        "stale_rows": len(rows),
        "drift_slots": slots,
        "issue_names": sorted(issues),
        "seen": seen,
    }


def _judge_models(claims_path: Path) -> list[dict[str, Any]]:
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
    with (REPO_ROOT / INVENTORY).open(encoding="utf-8-sig", newline="") as handle:
        return sorted({str(row["provider"]) for row in csv.DictReader(handle)})


def _provider_counts(models: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
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
) -> dict[str, Any]:
    providers = sorted(set(before) | set(after))
    accepted_before = {prov: before.get(prov, {}).get("accepted", 0) for prov in providers}
    accepted_after = {prov: after.get(prov, {}).get("accepted", 0) for prov in providers}
    movers = [prov for prov in providers if accepted_before[prov] != accepted_after[prov]]
    return {
        "tag": tag,
        "providers": providers,
        "rows_sum_after": sum(bucket["rows"] for bucket in after.values()),
        "accepted_sum_before": sum(accepted_before.values()),
        "accepted_sum_after": sum(accepted_after.values()),
        "movers": movers,
        "mover_deltas": {prov: accepted_after[prov] - accepted_before[prov] for prov in movers},
        "regressions": [
            f"{prov}:{accepted_before[prov]}->{accepted_after[prov]}"
            for prov in providers
            if accepted_after[prov] < accepted_before[prov]
        ],
        "accepted_before": accepted_before,
        "accepted_after": accepted_after,
    }


def _child_bogushosts(provider: str, model: str) -> dict[str, Any]:
    """One offline leg with the host grant forged to a sinkhole; did the seam notice?"""
    sys.path.insert(0, str(REPO_ROOT))
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "quality"))
    import iter2_claims_probe_runner as runner

    config = runner.witnesses.provider_config(provider)
    entry = dict(runner.witnesses.model_entry(provider, model))
    entry["hosts"] = [
        "sinkhole.invalid.example"
    ]  # forged: config says {sorted(config['allowed_hosts'])}
    entry["evidence_root_arg"] = str(LEGS_EVIDENCE_ROOT)
    entry["document"] = runner._fixture_document(LEGS_EVIDENCE_ROOT, provider, model)
    runner._block_real_sockets()
    rows, client, _fetcher = runner._run_direct_with_seam(provider, model, entry, entry["document"])
    return {
        "row": f"{provider}::{model}",
        "declared_hosts": sorted(str(host) for host in config["allowed_hosts"]),
        "forged_hosts": entry["hosts"],
        "seam_kind": entry["client_seam"]["kind"],
        "carrier_requests": len(client.calls),
        "rows_served": len(rows),
        "evidence_root": str(LEGS_EVIDENCE_ROOT),
    }


def _spawn_child(mode: str, *args: str) -> dict[str, Any]:
    rc, out = _run([sys.executable, str(Path(__file__).resolve()), "--child", mode, *args])
    for line in out.splitlines():
        if line.startswith("CHILD_JSON="):
            payload: dict[str, Any] = json.loads(line[len("CHILD_JSON=") :])
            payload["_child_exit"] = rc
            return payload
    raise RuntimeError(f"child mode {mode} answered nothing (exit={rc}): {out[-500:]}")


def main(argv: list[str] | None = None) -> int:
    """Print the wave-33 landing face and verdict on its arms."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", default=None)
    parser.add_argument("child_args", nargs="*")
    args = parser.parse_args(argv)

    if args.child == "bogushosts":
        print("CHILD_JSON=" + json.dumps(_child_bogushosts(args.child_args[0], args.child_args[1])))
        return 0

    arms: list[tuple[str, bool]] = []
    print(f"# carrier for {Path(__file__).name} (wave-33 landing radius and attribution)")
    print(f"# argv: {sys.executable} {Path(__file__).relative_to(REPO_ROOT)} {sys.argv[1:]}")
    print(
        f"# judge argv (disk): {sys.executable} scripts/quality/iter2_model_evidence.py"
        f" --inventory {INVENTORY} --claims {C101}/claims.merged.json"
        f" --output {SCRATCH}/judge.disk.json"
    )

    SCRATCH.mkdir(parents=True, exist_ok=True)
    head_raw = _git_show(f"{C101}/claims.merged.json")
    (SCRATCH / "claims.head.json").write_text(head_raw, encoding="utf-8")
    head_claims: list[dict[str, Any]] = json.loads(head_raw)["claims"]
    disk_claims: list[dict[str, Any]] = json.loads(
        (REPO_ROOT / C101 / "claims.merged.json").read_text(encoding="utf-8")
    )["claims"]
    pin_rows, pin_slots = _pin_census(head_claims, DOMAINS)
    print(
        f"A0_pin_table HEAD claims pin domains.yaml on {pin_rows}/{len(head_claims)} rows,"
        f" {pin_slots} byte-pin slots (read off the receipts, not asserted)"
    )

    pre = _judge(str(SCRATCH / "claims.head.json"), "judge.head.json")
    print(
        f"A_pre_chain_judged accepted={pre['accepted']}/{pre['identity_count']}"
        f" statuses={pre['statuses']} claims={pre['claim_count']}"
        f" identity_set={pre['identity_set_sha256']} judge_exit={pre['judge_exit']}"
    )
    arms.append(
        (
            "pre_chain_price_measured",
            pre["accepted"] == 0
            and pre["statuses"].get("STALE", 0) == pin_rows == 317
            and pre["claim_count"] == 1695,
        )
    )

    post = _judge(f"{C101}/claims.merged.json", "judge.disk.json")
    print(
        f"B_post_chain accepted={post['accepted']}/{post['identity_count']}"
        f" statuses={post['statuses']} claims={post['claim_count']}"
        f" identity_set={post['identity_set_sha256']} judge_exit={post['judge_exit']}"
    )
    zero_providers = sorted(
        prov for prov in _inventory_providers() if post["accepted_by_provider"].get(prov, 0) == 0
    )
    print(
        f"B0_coverage providers_with_accepted={len(post['accepted_by_provider'])}/32"
        f" zero_accepted={zero_providers}"
    )
    arms.append(
        (
            "post_chain_state",
            post["judge_exit"] == 0
            and post["accepted"] == 332
            and post["statuses"] == {"INCOMPLETE": 7, ACCEPTED: 332, "UNREVIEWED": 11}
            and post["claim_count"] == 1695
            and post["identity_set_sha256"] == "5168e1e723999312"
            and zero_providers == ["biztoc", "seeking_alpha", "stockgrid", "wsj"],
        )
    )

    walk_head = _pin_walk(head_claims, DOMAINS, None)
    walk_disk = _pin_walk(disk_claims, DOMAINS, None)
    forged = _pin_walk(disk_claims, DOMAINS, "0" * 64)
    print(
        f"C_drift head stale_rows={walk_head['stale_rows']}/{len(head_claims)}"
        f" disk stale_rows={walk_disk['stale_rows']} forged_digest stale_rows="
        f"{forged['stale_rows']}/{len(disk_claims)} issues={forged['issue_names']}"
        f" sample={sorted(walk_disk['issue_names'])}"
    )
    arms.append(
        (
            "drift_detector_bites",
            walk_head["stale_rows"] == 317
            and walk_disk["stale_rows"] == 0
            and forged["stale_rows"] == post["accepted"] > 0,
        )
    )

    expected_providers = _inventory_providers()
    inventory_rows = 0
    with (REPO_ROOT / INVENTORY).open(encoding="utf-8-sig", newline="") as handle:
        inventory_rows = sum(1 for _ in csv.DictReader(handle))
    before_models = _judge_models(SCRATCH / "claims.head.json")
    after_models = _judge_models(REPO_ROOT / C101 / "claims.merged.json")
    trap = _delta_face(
        "D_recomputed", _provider_counts(before_models), _provider_counts(after_models)
    )
    print(
        f"D_recomputed_trap accepted {trap['accepted_sum_before']} -> {trap['accepted_sum_after']}"
        f" movers={len(trap['movers'])} -- recomputing HEAD reads 0 accepted, so this table"
        " would call every lander a mover; it is printed to price the trap, not to attribute"
    )
    arms.append(
        (
            "trap_is_28_movers",
            len(trap["movers"]) == 28
            and trap["providers"] == expected_providers
            and trap["accepted_sum_before"] == 0
            and trap["rows_sum_after"] == inventory_rows == 350,
        )
    )

    landed = json.loads(_git_show(LANDED_REPORT))
    landed_models = [
        {str(key): value for key, value in entry.items()} for entry in landed["models"]
    ]
    face = _delta_face(
        "E_landed_report", _provider_counts(landed_models), _provider_counts(after_models)
    )
    print(
        f"E_landed_report file={LANDED_REPORT.split('/')[-1]} accepted"
        f" {face['accepted_sum_before']} -> {face['accepted_sum_after']}"
        f" movers={face['movers']} deltas={face['mover_deltas']}"
        f" regressions={face['regressions']}"
        f" tmx {face['accepted_before']['tmx']}->{face['accepted_after']['tmx']}"
        f" government_us {face['accepted_before']['government_us']}->"
        f"{face['accepted_after']['government_us']}"
    )
    arms.append(
        (
            "attribution_two_movers",
            face["accepted_sum_before"] == 317
            and face["accepted_sum_after"] == post["accepted"] == 332
            and face["movers"] == ["government_us", "tmx"]
            and face["mover_deltas"] == {"government_us": 1, "tmx": 14}
            and face["regressions"] == []
            and all(
                face["accepted_after"][prov] == post["accepted_by_provider"].get(prov, 0)
                for prov in _inventory_providers()
            ),
        )
    )

    forged_models = json.loads(json.dumps(after_models))
    flipped_row = ""
    for entry in sorted(forged_models, key=lambda item: str(item["model_identity"])):
        if entry.get("status") == ACCEPTED:
            entry["status"] = MISSED
            flipped_row = str(entry["model_identity"])
            break
    control = _delta_face(
        "F_control", _provider_counts(after_models), _provider_counts(forged_models)
    )
    recheck = _delta_face(
        "F_recheck",
        _provider_counts(landed_models),
        _provider_counts(_judge_models(REPO_ROOT / C101 / "claims.merged.json")),
    )
    print(
        f"F_control_forged row={flipped_row} status {ACCEPTED} -> {MISSED} (in memory only)"
        f" regressions={control['regressions']} accepted"
        f" {control['accepted_sum_before']} -> {control['accepted_sum_after']}"
        f" face_bites={bool(control['regressions'])}"
    )
    print(
        f"F_recheck_real accepted {recheck['accepted_sum_before']} ->"
        f" {recheck['accepted_sum_after']} movers={recheck['movers']}"
        f" regressions={recheck['regressions']}"
        f" identical_to_E={recheck['accepted_after'] == face['accepted_after']}"
    )
    arms.append(
        (
            "attribution_arm_falsifies",
            bool(control["regressions"])
            and control["accepted_sum_after"] == 331
            and recheck["regressions"] == []
            and recheck["movers"] == face["movers"]
            and recheck["accepted_after"] == face["accepted_after"],
        )
    )

    census_rc, census_out = _run(
        [sys.executable, str(REPO_ROOT / CENSUS)],
        env={**dict(os.environ), "PYTHONPATH": str(REPO_ROOT)},
    )
    census_face = next(
        (line for line in census_out.splitlines() if "residue_axis_census_face" in line), ""
    )
    actions_line = next((line for line in census_out.splitlines() if "2_actions" in line), "")
    failed_census_arms = [
        line.split()[0]
        for line in census_out.splitlines()
        if re.match(r"^[A-H]_", line) and "PASS=False" in line
    ]
    balance_landed = any(
        entry.get("status") == ACCEPTED
        and f"{entry.get('provider')}::{entry.get('upstream_model')}" == "yfinance::BalanceSheet"
        for entry in after_models
    )
    print(f"G_census_rerun exit={census_rc} {census_face}")
    print(f"G_census_actions {actions_line}")
    print(
        f"G_census_red_arm failing={failed_census_arms} cause=yfinance::BalanceSheet landed"
        f" (accepted={balance_landed}) -- wave 32 consumed the shipped census's hardcoded"
        " negative control; the census bytes were not touched by this probe"
    )
    arms.append(
        (
            "census_flip_residue_18",
            census_rc == 1
            and "residue=18" in census_face
            and "'implement_missing_model': 4" in actions_line
            and "sums=18" in actions_line
            and failed_census_arms == ["B_landed_negative"]
            and balance_landed,
        )
    )

    head_ops = re.findall(r'^    "([A-Za-z0-9]+)": \(', _git_show(TMX_CLIENT), re.M)
    sys.path.insert(0, str(REPO_ROOT))
    from opendata.data.providers.tmx.models import client as tmx_client

    disk_ops = sorted(tmx_client._OPERATIONS)
    new_ops = sorted(set(disk_ops) - set(head_ops))
    refusal = ""
    try:
        tmx_client.graphql("dropEverythingForever", {})
        refusal = "NO_REFUSAL"
    except Exception as exc:
        refusal = f"{type(exc).__name__}:{exc}"
    domains_head = len(re.findall(r"^  [a-z0-9_]+:", _git_show(DOMAINS), re.M))
    domains_disk = len(re.findall(r"^  [a-z0-9_]+:", (REPO_ROOT / DOMAINS).read_text(), re.M))
    print(
        f"H_graphql_gate operations {len(head_ops)} -> {len(disk_ops)} new={new_ops}"
        f" unregistered_live_call={refusal} allow_list_is_an_enum_not_a_wildcard="
        f"{disk_ops != ['*'] and len(disk_ops) < 20}"
    )
    print(
        f"H_domains_radius keys {domains_head} -> {domains_disk} (+{domains_disk - domains_head})"
    )
    arms.append(
        (
            "allow_list_still_bites",
            len(head_ops) == 7
            and len(disk_ops) == 11
            and len(new_ops) == 4
            and refusal.startswith("TmxProviderError:TMX_OPERATION_NOT_ALLOWED")
            and domains_head == 338
            and domains_disk == 353,
        )
    )

    legs_roots: list[str] = []
    leg_faces: list[dict[str, str]] = []
    for provider, model in LEG_ROWS:
        source = REPO_ROOT / C101 / "fixtures" / provider / f"{model}.json"
        target = LEGS_EVIDENCE_ROOT / "fixtures" / provider / f"{model}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        rc, out = _run(
            [
                sys.executable,
                str(REPO_ROOT / RUNNER),
                "--provider",
                provider,
                "--model",
                model,
                "--phase",
                "counterfact",
                "--evidence-root",
                str(LEGS_EVIDENCE_ROOT),
            ]
        )
        legs_roots.append(str(LEGS_EVIDENCE_ROOT))
        payload: dict[str, Any] = next(
            (
                json.loads(line[len("COUNTERFACT_JSON=") :])
                for line in out.splitlines()
                if line.startswith("COUNTERFACT_JSON=")
            ),
            {},
        )
        reason = str(payload.get("reason", ""))
        site = next((token for token in re.findall(r"opendata/[\w/.-]+\.py", reason)), "")
        if not site:
            fetcher_match = re.search(r"([A-Z][A-Za-z]+Fetcher)\.(\w+)", reason)
            if fetcher_match:
                _grep_rc, grep_out = _run(
                    [
                        "grep",
                        "-rl",
                        f"class {fetcher_match.group(1)}",
                        f"opendata/data/providers/{provider}",
                    ]
                )
                found = next((line.strip() for line in grep_out.splitlines() if line.strip()), "")
                site = (
                    f"{found}::{fetcher_match.group(1)}.{fetcher_match.group(2)}"
                    if found
                    else "site-not-named"
                )
            else:
                site = "site-not-named"
        observed = payload.get("observed", {})
        leg_faces.append(
            {
                "row": f"{provider}::{model}",
                "exit": str(rc),
                "code": str(observed.get("refusal_code")),
                "cls": str(observed.get("refusal_class")),
                "site": site,
            }
        )
        verdict_line = next(
            (line for line in out.splitlines() if line.startswith("VERDICT=")), "NONE"
        )
        print(
            f"I_counterfact_leg row={provider}::{model} exit={rc}"
            f" verdict={verdict_line}"
            f" refusal={observed.get('refusal_class')}:{observed.get('refusal_code')}"
            f" site={site} evidence_root={LEGS_EVIDENCE_ROOT}"
        )
        print(f"I_refusal_reason_head {reason[:220]}")
    c101_seen = str(REPO_ROOT / C101) in legs_roots
    print(
        f"I_leg_roots legs={len(legs_roots)} distinct={sorted(set(legs_roots))}"
        f" c101_root_seen={c101_seen}"
    )
    arms.append(
        (
            "refusal_shape_replayed",
            all(face_leg["exit"] == "0" and face_leg["code"] for face_leg in leg_faces)
            and {face_leg["code"] for face_leg in leg_faces}
            == {"TMX_BAD_ROWS", "TREASURY_PRICE_BAD_VALUE"}
            and leg_faces[0]["site"] == "opendata/data/providers/tmx/models/fetchers_w33_graphql.py"
            and "treasury_prices.py" in leg_faces[1]["site"]
            and not c101_seen
            and not str(LEGS_EVIDENCE_ROOT).startswith(str(REPO_ROOT)),
        )
    )

    bogus = _spawn_child("bogushosts", "government_us", "TreasuryPrices")
    budget_line = next(
        (
            (number, line.strip())
            for number, line in enumerate(
                (REPO_ROOT / BUDGET).read_text(encoding="utf-8").splitlines(), start=1
            )
            if "host not in grant.allowed_hosts" in line
        ),
        (0, "not-found"),
    )
    print(
        f"J_offline_leg_hosts row={bogus['row']} kind={bogus['seam_kind']}"
        f" declared={bogus['declared_hosts']} forged={bogus['forged_hosts']}"
        f" requests={bogus['carrier_requests']} rows_served={bogus['rows_served']}"
        f" child_exit={bogus['_child_exit']}"
    )
    print(
        f"J_real_path {BUDGET}:{budget_line[0]} `{budget_line[1]}` -- the grant the witness"
        " builds at iter2_claims_probe_runner.py:152 is never consulted by the seam fake"
    )
    arms.append(
        (
            "host_blind_spot_measured",
            bogus["rows_served"] > 0
            and bogus["_child_exit"] == 0
            and budget_line[0] > 0
            and "host not in grant.allowed_hosts" in budget_line[1],
        )
    )

    runs = REPO_ROOT / BUNDLE / "runs"
    files = sorted(path.name for path in runs.glob("*.txt"))
    walked = [name for name in files if name != SELF_CARRIER]
    bodies = {name: (runs / name).read_text(encoding="utf-8") for name in walked}
    missing_exit = [
        name
        for name in walked
        if not (
            (lines := [line for line in bodies[name].splitlines() if line.strip()])
            and CARRIER_EXIT_LINE.search(lines[-1])
        )
    ]
    joined = "\n".join(bodies.values())
    chain_exits = re.findall(r"\bchain_\w*_exit\s*=\s*(-?\d+)", joined)
    negatives = sorted(set(re.findall(r"\b\w*_(?:rc|exit)\s*=\s*-\d+", joined)))
    summary = bodies.get("chain_summary.txt", "")
    recorders: dict[str, tuple[int, int]] = {}
    for name in ("recorder_tmx.txt", "recorder_government_us.txt"):
        match = re.search(r"signed=(\d+) refused=(\d+)", bodies.get(name, ""))
        recorders[name] = (int(match.group(1)), int(match.group(2))) if match else (-1, -1)
    summary_flags = {
        "nonzero_members_empty": ("nonzero_members=[]" in summary)
        or ('"nonzero_members": []' in summary),
        "merge_idempotent": ("merge_idempotent_stable_payload=True" in summary)
        or ('"merge_idempotent_stable_payload": true' in summary),
        "stable_sha_hits": summary.count("21b59eebf5974133"),
    }
    print(
        f"K_carriers population={BUNDLE}/runs/*.txt files={len(files)} walked={len(walked)}"
        f" self_carrier_excluded={SELF_CARRIER} missing_exit_line={missing_exit}"
        f" chain_exit_readings={len(chain_exits)} chain_exit_nonzero="
        f"{sorted({value for value in chain_exits if value != '0'})}"
        f" negative_readings={negatives} summary_flags={summary_flags}"
        f" recorders={recorders}"
    )
    arms.append(
        (
            "carriers_signed",
            bool(
                missing_exit == []
                and len(walked) == 60
                and chain_exits
                and all(value == "0" for value in chain_exits)
                and negatives == []
                and summary_flags["nonzero_members_empty"]
                and summary_flags["merge_idempotent"]
                and summary_flags["stable_sha_hits"] >= 2
                and recorders.get("recorder_tmx.txt") == (22, 2)
                and recorders.get("recorder_government_us.txt") == (6, 0)
            ),
        )
    )

    passed = sum(1 for _, ok in arms if ok)
    failed = [name for name, ok in arms if not ok]
    print(
        f"landing_radius_face arms={len(arms)} arms_passed={passed} arms_failed={len(failed)}"
        f" failed={failed} pre_chain_accepted={pre['accepted']}"
        f" post_chain_accepted={post['accepted']} movers={face['mover_deltas']}"
        f" census_residue=33->{18 if 'residue=18' in census_face else -1}"
        f" identity_set={post['identity_set_sha256']} PASS={not failed}"
    )
    rc = 0 if not failed else 1
    print(f"w33_landing_radius_exit={rc}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
