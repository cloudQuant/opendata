"""Wave-32 seam liveness: is ``governed_sdk_session`` a measurement or a switch?

Four things have to be readable off disk and off a running process for the new witness seam kind
to mean anything:

* the five landed phases really pass on rows that ride it (two different SDK surfaces, both
  chosen here, not granted by the chain);
* the fake is asked, and the answer the fetch got is the fake's -- the carrier's own request and
  invocation logs are read off the leg that ran, so a seam that never got used cannot report a
  clean run;
* the harness *refuses* when the leg does not ask (``_seam_guard``) and when the tampered leg is
  served from the control's cache (the ``--tampered-leg-child`` isolation and its
  ``seam_requests >= 1`` assertion) -- forged legs, so the two new gates are shown to bite;
* the other seam kind is untouched: ``nasdaq::EquitySearch`` is a landed ``shared_http_client``
  row that is not part of this wave, and it is used here as the negative control.

Nothing here writes a pinned artifact. The judge is imported in-process so the counters on the
face line are same-generation as the claims file they count.

The five-phase legs re-enter ``iter2_claims_probe_runner.py`` with ``--evidence-root``, and that
root is the *scratch* tree this probe builds under ``/tmp`` -- never ``docs/evidence/C101``, whose
317 landed receipts carry ``executed_at_utc`` stamps the judge pins: re-running a phase into that
tree would re-age the artifacts and stale the landing set this wave just produced. The fixtures
the rows need are copied in by digest, every leg prints the root it ran under, and the carrier
closes with a before/after witness over ``C101/receipts`` and ``C101/captures`` (``git status
--porcelain`` symmetric difference plus a ``find -newer <marker>`` reading) that has to show
nothing moved.

Child modes (this file re-enters itself; ``YfData`` is a process singleton, so every arm that
touches the SDK has to own a process):

    --child real <provider> <model>     run one real leg, print the carrier's logs
    --child masked <provider> <model>   forge the pre-fix shared-process tampered leg
    --child isolated <provider> <model> run the shipped isolated tampered leg
    --child forgedchild <provider> <model>  answer the parent with seam_requests=0
    --child guardfaces                  the three _seam_guard forgeries
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import shutil
import subprocess  # nosec B404  # read-only evidence probe; argv is always a literal list
import sys
from pathlib import Path
from typing import Any, Final

REPO_ROOT: Final = Path(__file__).resolve().parents[5]
BUNDLE: Final = "docs/evidence/C137/wave32-yfinance-landing-20261018"
EVIDENCE_ROOT: Final = "docs/evidence/C101"
JUDGE: Final = "scripts/quality/iter2_model_evidence.py"
RUNNER: Final = "scripts/quality/iter2_claims_probe_runner.py"
INVENTORY: Final = "docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/模型级任务清单.csv"
SCRATCH: Final = Path("/tmp/w32_seam_liveness")  # noqa: S108  # judge --output only, never a pinned path
#: Scratch evidence root the five-phase legs run against; the runner resolves
#: ``REPO_ROOT / args.evidence_root``, so an absolute path lands outside the repo and C101 is
#: never a write target.
LEGS_EVIDENCE_ROOT: Final = SCRATCH / "evidence_root"
C101_TREES: Final = (f"{EVIDENCE_ROOT}/receipts", f"{EVIDENCE_ROOT}/captures")

PHASES: Final = ("implementation", "direct", "service", "contract", "counterfact")
#: Two landed yfinance rows on different SDK surfaces: a chart leg (``/v8/finance/chart``) and a
#: screener leg (``/v1/finance/screener/saved/query``).
SURFACES: Final = (("yfinance", "EquityHistorical"), ("yfinance", "EquityScreener"))
NEGATIVE: Final = ("nasdaq", "EquitySearch")
#: Every identity this probe drives a leg for -- the fixture population of the scratch root.
LEG_ROWS: Final = tuple(sorted({*SURFACES, NEGATIVE, ("yfinance", "BalanceSheet")}))


def _run(argv: list[str]) -> tuple[int, str]:
    proc = subprocess.run(  # noqa: S603  # nosec B603  # literal argv, no shell, no user input
        argv, capture_output=True, text=True, cwd=REPO_ROOT, check=False, timeout=600
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _leg_argv(provider: str, model: str, phase: str) -> list[str]:
    """One phase leg; its ``--evidence-root`` is always the scratch tree, never C101."""
    return [
        sys.executable,
        str(REPO_ROOT / RUNNER),
        "--provider",
        provider,
        "--model",
        model,
        "--phase",
        phase,
        "--evidence-root",
        str(LEGS_EVIDENCE_ROOT),
    ]


def _prepare_scratch() -> dict[str, Any]:
    """Copy the fixtures the legs read into the scratch root; prove the bytes are the same."""
    scratch_parent = str(SCRATCH.parent)
    root_is_scratch = str(LEGS_EVIDENCE_ROOT).startswith(f"{scratch_parent}/") and not str(
        LEGS_EVIDENCE_ROOT
    ).startswith(str(REPO_ROOT))
    if not root_is_scratch:
        raise RuntimeError(f"refusing to run legs against a non-scratch root {LEGS_EVIDENCE_ROOT}")
    SCRATCH.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for provider, model in LEG_ROWS:
        source = REPO_ROOT / EVIDENCE_ROOT / "fixtures" / provider / f"{model}.json"
        target = LEGS_EVIDENCE_ROOT / "fixtures" / provider / f"{model}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        rows.append(
            {
                "row": f"{provider}::{model}",
                "source": source.relative_to(REPO_ROOT).as_posix(),
                "source_sha": _sha_bytes(source),
                "target_sha": _sha_bytes(target),
            }
        )
    return {
        "root": str(LEGS_EVIDENCE_ROOT),
        "outside_repo": root_is_scratch,
        "rows": rows,
        "digests_match": all(row["source_sha"] == row["target_sha"] for row in rows),
    }


def _c101_witness(marker: Path) -> dict[str, Any]:
    """Read C101's receipts and captures the way a tamper would show itself: by name and mtime."""
    git_rc, porcelain = _run(["git", "status", "--porcelain", "--", *C101_TREES])
    lines = sorted(line for line in porcelain.splitlines() if line.strip())
    find_rc, newer = _run(["find", *C101_TREES, "-type", "f", "-newer", str(marker)])
    touched = sorted(line for line in newer.splitlines() if line.strip())
    _, listed = _run(["find", *C101_TREES, "-type", "f"])
    _, scratch_newer = _run(["find", str(LEGS_EVIDENCE_ROOT), "-type", "f", "-newer", str(marker)])
    return {
        "lines": lines,
        "git_rc": git_rc,
        "find_rc": find_rc,
        "porcelain_lines": len(lines),
        "porcelain_sha": _sha("\n".join(lines)),
        "tracked_files": len([line for line in listed.splitlines() if line.strip()]),
        "newer_than_marker": len(touched),
        "newer_sample": touched[:4],
        "scratch_new_files": len([line for line in scratch_newer.splitlines() if line.strip()]),
        "scratch_new_sample": sorted(line for line in scratch_newer.splitlines() if line.strip())[
            :3
        ],
    }


def _phase_passes(provider: str, model: str) -> tuple[int, int, str, str]:
    """Run all five phases for one identity; return (passes, runs, verdicts, root the legs used)."""
    passes = 0
    notes: list[str] = []
    roots: list[str] = []
    for phase in PHASES:
        argv = _leg_argv(provider, model, phase)
        roots.append(argv[-1])
        rc, out = _run(argv)
        verdict = next((line for line in out.splitlines() if line.startswith("VERDICT=")), "NONE")
        passes += 1 if rc == 0 and verdict == "VERDICT=PASS" else 0
        notes.append(f"{phase}:{verdict.split('=')[-1]}")
    return passes, len(PHASES), ",".join(notes), roots[0]


def _child_runner_argv(provider: str, model: str) -> Any:  # noqa: ANN401  # the runner module and its fixture doc are dynamic
    sys.path.insert(0, str(REPO_ROOT))
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "quality"))
    import iter2_claims_probe_runner as runner

    config = runner.witnesses.provider_config(provider)
    entry = dict(runner.witnesses.model_entry(provider, model))
    entry["hosts"] = config["allowed_hosts"]
    entry["evidence_root_arg"] = str(LEGS_EVIDENCE_ROOT)
    import os

    for name, value in config.get("credential_env", {}).items():
        if not os.environ.get(name):
            os.environ[name] = str(value)
    entry["document"] = runner._fixture_document(LEGS_EVIDENCE_ROOT, provider, model)
    return runner, entry


def _child_real(provider: str, model: str) -> dict[str, Any]:
    """One honest leg, with the carrier's own counters read back."""
    runner, entry = _child_runner_argv(provider, model)
    runner._block_real_sockets()
    handle: list[Any] = []
    rows, _client, fetcher_type = runner._run_direct_with_seam(
        provider, model, entry, entry["document"], handle=handle
    )
    client = handle[0]
    guard = "passes"
    try:
        runner._seam_guard(client, leg="direct")
    except Exception as exc:
        guard = f"refuses:{exc}"
    invoked = getattr(client, "invoked", None)
    return {
        "provider_model": f"{provider}::{model}",
        "seam_kind": entry["client_seam"]["kind"],
        "seam_attr": entry["client_seam"]["attr"],
        "evidence_root": str(entry.get("evidence_root_arg")),
        "fake_class": f"{type(client).__module__}.{type(client).__name__}",
        "fake_is_curl_cffi_session": type(client).__mro__[1].__name__,
        "carrier_requests": len(client.calls),
        "carrier_invocations": len(invoked or []),
        "invoked_sdk_object": (
            "n/a" if not invoked else f"{type(invoked[0]).__module__}.{type(invoked[0]).__name__}"
        ),
        "rows": len(rows),
        "row_class": fetcher_type.row_model.__name__,
        "guard_on_real_leg": guard,
    }


def _child_guardfaces() -> dict[str, Any]:
    """The three forgeries that ``_seam_guard`` has to refuse."""
    runner, entry = _child_runner_argv("yfinance", "EquityHistorical")
    readings: list[str] = []
    cases: list[tuple[str, list[tuple[str, str]], list[Any]]] = [
        ("never_asked_direct", [], []),
        ("asked_but_never_invoked_the_attr", [("GET", "x")], []),
        ("tampered_leg_masked_by_cache", [], []),
    ]
    for name, calls, invoked in cases:
        carrier = runner._build_sdk_session_fake([{"url_contains": "never-matches", "body": {}}])
        carrier.calls = list(calls)
        carrier.invoked = list(invoked)
        leg = runner.TAMPERED_LEG if name.endswith("masked_by_cache") else "direct"
        try:
            runner._seam_guard(carrier, leg=leg)
            readings.append(f"{name}=NO_REFUSAL")
        except runner.WitnessFailure as failure:
            readings.append(f"{name}=refused({str(failure)[:48]}...)")
    live = runner._build_sdk_session_fake([{"url_contains": "x", "body": {}}])
    live.calls = [("GET", "x")]
    live.invoked = [object()]
    with_live = "passes"
    try:
        runner._seam_guard(live)
    except Exception as exc:
        with_live = f"refuses:{exc}"
    return {
        "refusals": f"{sum('refused' in r for r in readings)}/{len(cases)}",
        "readings": readings,
        "honest_leg_guard": with_live,
        "seam_attr_of_this_row": entry["client_seam"]["attr"],
        "evidence_root": str(entry.get("evidence_root_arg")),
    }


def _child_masked(provider: str, model: str) -> dict[str, Any]:
    """The hole, re-opened: the tampered leg forced back into the control's process."""
    runner, entry = _child_runner_argv(provider, model)
    runner._block_real_sockets()
    seen: dict[str, Any] = {}

    def _shared(p: str, m: str, ent: dict[str, Any], doc: Any) -> dict[str, Any]:  # noqa: ANN401
        report = runner._run_tampered_leg(p, m, ent, doc)
        try:
            from yfinance.data import YfData

            seen["cache_info"] = str(YfData.cache_get.cache_info())
        except Exception as exc:
            seen["cache_info"] = f"unavailable:{type(exc).__name__}"
        seen["report"] = dict(report)
        return dict(report)

    original = runner._spawn_tampered_leg
    runner._spawn_tampered_leg = _shared
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer):
            rc = runner.main(
                [
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
    finally:
        runner._spawn_tampered_leg = original
    out = buffer.getvalue()
    reason = next((line for line in out.splitlines() if line.startswith("VERDICT=")), "NONE")
    report = seen.get("report", {})
    return {
        "phase_exit": rc,
        "evidence_root": str(entry.get("evidence_root_arg")),
        "verdict": reason,
        "tampered_leg_seam_requests": report.get("seam_requests"),
        "tampered_leg_rows": report.get("rows"),
        "tampered_leg_outcome": report.get("outcome"),
        "yfd_lru": seen.get("cache_info"),
    }


def _child_isolated(provider: str, model: str) -> dict[str, Any]:
    """The shipped path: the tampered leg in a fresh child of the same interpreter."""
    runner, entry = _child_runner_argv(provider, model)
    runner._block_real_sockets()
    tampered = runner._apply_tamper(entry["document"], entry["tamper"])
    control_rows, _, _ = runner._run_direct_with_seam(provider, model, entry, entry["document"])
    report = runner._spawn_tampered_leg(provider, model, entry, tampered)
    return {
        "evidence_root": str(entry.get("evidence_root_arg")),
        "control_rows": len(control_rows),
        "tampered_leg_seam_requests": report.get("seam_requests"),
        "tampered_leg_outcome": report.get("outcome"),
        "tampered_leg_refusal_code": report.get("observed", {}).get("refusal_code"),
    }


def _child_forgedchild(provider: str, model: str) -> dict[str, Any]:
    """Answer the parent like a cached leg would: a refusal with zero requests."""
    runner, entry = _child_runner_argv(provider, model)
    runner._block_real_sockets()
    forged = {
        "outcome": "refusal",
        "seam_requests": 0,
        "reason": "forged: refusal face present, request log empty",
        "observed": {
            "refusal_class": entry["refusal_class"],
            "refusal_code": entry["refusal_code"],
            "status": entry.get("expected_status"),
            "rejected": list(entry.get("expected_rejected", [])),
            "missing": list(entry.get("expected_missing", [])),
            "invalid": list(entry.get("expected_invalid", [])),
        },
    }
    runner._spawn_tampered_leg = lambda *a, **k: dict(forged)
    root = str(entry.get("evidence_root_arg"))
    try:
        runner.witness_counterfact(provider, model, entry)
        return {
            "refused": False,
            "evidence_root": root,
            "reason": "NO_REFUSAL: the gate reads the outcome field only",
        }
    except runner.WitnessFailure as failure:
        return {"refused": True, "evidence_root": root, "reason": str(failure)}


def _child_negative_control() -> dict[str, Any]:
    """The other seam kind, measured: no ``invoked`` face exists for it to grow."""
    provider, model = NEGATIVE
    runner, entry = _child_runner_argv(provider, model)
    runner._block_real_sockets()
    handle: list[Any] = []
    rows, _client, fetcher_type = runner._run_direct_with_seam(
        provider, model, entry, entry["document"], handle=handle
    )
    client = handle[0]
    return {
        "provider_model": f"{provider}::{model}",
        "seam_kind": entry["client_seam"]["kind"],
        "carrier_class": f"{type(client).__module__}.{type(client).__name__}",
        "carrier_has_invoked_face": hasattr(client, "invoked"),
        "evidence_root": str(entry.get("evidence_root_arg")),
        "carrier_requests": len(client.calls),
        "rows": len(rows),
        "row_class": fetcher_type.row_model.__name__,
    }


def _child_stdout(provider: str, model: str, phase: str) -> str:
    rc, out = _run(_leg_argv(provider, model, phase))
    if rc != 0:
        return f"exit={rc}\n{out}"
    return out


def _spawn_child(mode: str, *args: str) -> dict[str, Any]:
    rc, out = _run([sys.executable, str(Path(__file__).resolve()), "--child", mode, *args])
    for line in out.splitlines():
        if line.startswith("CHILD_JSON="):
            payload: dict[str, Any] = json.loads(line[len("CHILD_JSON=") :])
            payload["_child_exit"] = rc
            return payload
    raise RuntimeError(f"child mode {mode} answered nothing (exit={rc}): {out[-400:]}")


def _judge_in_process() -> dict[str, Any]:
    """Run the repo's own judge with --output under /tmp; read its counters back."""
    sys.path.insert(0, str(REPO_ROOT))
    from scripts.quality import iter2_model_evidence as judge

    SCRATCH.mkdir(parents=True, exist_ok=True)
    inventory = judge.load_inventory(REPO_ROOT / INVENTORY)
    rc = judge.main(
        [
            "--inventory",
            INVENTORY,
            "--claims",
            f"{EVIDENCE_ROOT}/claims.merged.json",
            "--output",
            str(SCRATCH / "judge.json"),
        ]
    )
    report = json.loads((SCRATCH / "judge.json").read_text(encoding="utf-8"))
    claims = json.loads((REPO_ROOT / EVIDENCE_ROOT / "claims.merged.json").read_text())["claims"]
    statuses: dict[str, int] = {}
    for model in report["models"]:
        statuses[model["status"]] = statuses.get(model["status"], 0) + 1
    return {
        "judge_exit": rc,
        "expected_identity_count": judge.EXPECTED_MODEL_COUNT,
        "required_phases": len(judge.REQUIRED_PHASES),
        "identity_set_sha256": report["inventory"]["identity_set_sha256"][:16],
        "judge_identity_projection": judge.identity_projection_sha256(
            [(row["provider"], row["upstream_model"]) for row in inventory["rows"]]
        )[:16],
        "inventory_rows": len(inventory["rows"]),
        "claims_file_identity": json.loads(
            (REPO_ROOT / EVIDENCE_ROOT / "claims.merged.json").read_text()
        )["identity_set_sha256"][:16],
        "claim_count": report["summary"]["claim_count"],
        "claims_in_file": len(claims),
        "identity_count": report["summary"]["identity_count"],
        "offline_acceptance_count": report["summary"]["offline_acceptance_count"],
        "statuses": statuses,
    }


def main(argv: list[str] | None = None) -> int:
    """Run every arm in its own process where that is what the reading needs, and verdict."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", default=None)
    parser.add_argument("child_args", nargs="*")
    args = parser.parse_args(argv)

    if args.child:
        mode = args.child
        extra = args.child_args
        if mode == "real":
            payload = _child_real(extra[0], extra[1])
        elif mode == "guardfaces":
            payload = _child_guardfaces()
        elif mode == "masked":
            payload = _child_masked(extra[0], extra[1])
        elif mode == "isolated":
            payload = _child_isolated(extra[0], extra[1])
        elif mode == "forgedchild":
            payload = _child_forgedchild(extra[0], extra[1])
        elif mode == "negative":
            payload = _child_negative_control()
        else:
            raise SystemExit(f"unknown child mode {mode!r}")
        print("CHILD_JSON=" + json.dumps(payload, sort_keys=True, default=str))
        return 0

    arms: list[tuple[str, bool]] = []
    print(f"# carrier for {Path(__file__).name} (wave-32 seam liveness)")
    print(f"# argv: {sys.executable} {Path(__file__).relative_to(REPO_ROOT)} {sys.argv[1:]}")
    print(
        f"# judge argv: {sys.executable} {JUDGE} --inventory {INVENTORY} "
        f"--claims {EVIDENCE_ROOT}/claims.merged.json --output {SCRATCH}/judge.json"
    )
    print("# every arm is read off a process or off disk in this run; nothing here is copied")
    print(
        f"# legs argv: {sys.executable} {RUNNER} --provider <p> --model <m> --phase <phase>"
        f" --evidence-root {LEGS_EVIDENCE_ROOT}  (scratch; C101 is never a write target here)"
    )

    scratch = _prepare_scratch()
    marker = SCRATCH / "legs-marker"
    marker.write_text(f"{scratch['root']}\n", encoding="utf-8")
    before = _c101_witness(marker)
    print(
        f"scratch_root root={scratch['root']} fixtures_copied={len(scratch['rows'])}"
        f" rows={[row['row'] for row in scratch['rows']]}"
        f" digests_match={scratch['digests_match']} outside_repo={scratch['outside_repo']}"
    )
    for row in scratch["rows"]:
        print(
            f"scratch_fixture row={row['row']} source={row['source']}"
            f" source_sha={row['source_sha']} target_sha={row['target_sha']}"
        )
    print(
        f"c101_witness_before files={before['tracked_files']}"
        f" porcelain_lines={before['porcelain_lines']} porcelain_sha={before['porcelain_sha']}"
        f" newer_than_marker={before['newer_than_marker']} git_rc={before['git_rc']}"
        f" find_rc={before['find_rc']}"
    )

    gen = _judge_in_process()
    print(
        f"generation judge_exit={gen['judge_exit']} identities={gen['identity_count']}"
        f"/{gen['expected_identity_count']} accepted={gen['offline_acceptance_count']}"
        f" claims={gen['claim_count']} required_phases={gen['required_phases']}"
        f" identity_set={gen['identity_set_sha256']}"
        f" identity_projection={gen['judge_identity_projection']}"
        f" claims_file_identity={gen['claims_file_identity']} statuses={gen['statuses']}"
    )
    arms.append(
        ("judge_generation", gen["judge_exit"] == 0 and gen["claim_count"] == gen["claims_in_file"])
    )

    total_runs = 0
    total_passes = 0
    leg_roots: list[str] = []
    for provider, model in SURFACES:
        passes, runs, notes, root = _phase_passes(provider, model)
        leg_roots.append(root)
        total_runs += runs
        total_passes += passes
        print(
            f"phases row={provider}::{model} runs={runs} passes={passes}"
            f" verdicts={notes} evidence_root={root}"
        )
        arms.append((f"phases_{model}", passes == runs))

    for provider, model in SURFACES:
        real = _spawn_child("real", provider, model)
        leg_roots.append(str(real["evidence_root"]))
        print(
            f"seam_face row={real['provider_model']} kind={real['seam_kind']}"
            f" attr={real['seam_attr']} fake={real['fake_class']}"
            f" mro1={real['fake_is_curl_cffi_session']}"
            f" evidence_root={real['evidence_root']}"
            f" requests={real['carrier_requests']} invocations={real['carrier_invocations']}"
            f" invoked_sdk={real['invoked_sdk_object']} rows={real['rows']}"
            f" row_class={real['row_class']} guard={real['guard_on_real_leg']}"
        )
        arms.append(
            (
                f"seam_asked_{model}",
                int(real["carrier_requests"]) >= 1
                and int(real["carrier_invocations"]) >= 1
                and real["guard_on_real_leg"] == "passes"
                and int(real["rows"]) >= 1,
            )
        )

    guards = _spawn_child("guardfaces")
    leg_roots.append(str(guards["evidence_root"]))
    for reading in guards["readings"]:
        print(f"guard_forge {reading}")
    print(
        f"guard_face refusals={guards['refusals']} honest_leg_guard={guards['honest_leg_guard']}"
        f" evidence_root={guards['evidence_root']} child_exit={guards['_child_exit']}"
    )
    arms.append(
        (
            "seam_guard_bites",
            guards["refusals"] == "3/3" and guards["honest_leg_guard"] == "passes",
        )
    )

    masked = _spawn_child("masked", "yfinance", "BalanceSheet")
    leg_roots.append(str(masked["evidence_root"]))
    print(
        f"hole_shared_process row=yfinance::BalanceSheet exit={masked['phase_exit']}"
        f" verdict={masked['verdict']}"
        f" tampered_requests={masked['tampered_leg_seam_requests']}"
        f" tampered_rows={masked['tampered_leg_rows']} yfd_lru={masked['yfd_lru']}"
        f" evidence_root={masked['evidence_root']}"
    )
    isolated = _spawn_child("isolated", "yfinance", "BalanceSheet")
    leg_roots.append(str(isolated["evidence_root"]))
    print(
        f"closure_child_isolation row=yfinance::BalanceSheet"
        f" control_rows={isolated['control_rows']}"
        f" tampered_requests={isolated['tampered_leg_seam_requests']}"
        f" tampered_outcome={isolated['tampered_leg_outcome']}"
        f" refusal_code={isolated['tampered_leg_refusal_code']}"
        f" evidence_root={isolated['evidence_root']}"
    )
    forged = _spawn_child("forgedchild", "yfinance", "BalanceSheet")
    leg_roots.append(str(forged["evidence_root"]))
    print(
        f"gate_reads_requests refused={forged['refused']}"
        f" evidence_root={forged['evidence_root']}"
        f" reason={forged['reason'][:120]}"
    )
    arms.append(
        (
            "cache_masking_closed",
            int(masked["tampered_leg_seam_requests"] or 0) == 0
            and int(masked["phase_exit"]) == 1
            and int(isolated["tampered_leg_seam_requests"] or 0) >= 1
            and isolated["tampered_leg_outcome"] == "refusal"
            and forged["refused"] is True,
        )
    )

    neg_passes, neg_runs, neg_notes, neg_root = _phase_passes(*NEGATIVE)
    leg_roots.append(neg_root)
    print(
        f"negative_control row={NEGATIVE[0]}::{NEGATIVE[1]} runs={neg_runs} passes={neg_passes}"
        f" verdicts={neg_notes} evidence_root={neg_root}"
    )
    neg = _spawn_child("negative")
    leg_roots.append(str(neg["evidence_root"]))
    print(
        f"other_kind row={neg['provider_model']} kind={neg['seam_kind']}"
        f" carrier={neg['carrier_class']}"
        f" has_invoked_face={neg['carrier_has_invoked_face']}"
        f" requests={neg['carrier_requests']}"
        f" rows={neg['rows']} row_class={neg['row_class']}"
        f" evidence_root={neg['evidence_root']}"
    )
    repeat_a = _child_stdout(*NEGATIVE, "direct")
    repeat_b = _child_stdout(*NEGATIVE, "direct")
    repeat_c = _child_stdout(*NEGATIVE, "service")
    repeat_d = _child_stdout(*NEGATIVE, "service")
    leg_roots.append(_leg_argv(NEGATIVE[0], NEGATIVE[1], "direct")[-1])
    print(
        f"other_kind_payload direct_identical={repeat_a == repeat_b}"
        f" service_identical={repeat_c == repeat_d} direct_sha={_sha(repeat_a)}"
        f" service_sha={_sha(repeat_c)}"
    )
    claims = json.loads((REPO_ROOT / EVIDENCE_ROOT / "claims.merged.json").read_text())["claims"]
    disk = [c for c in claims if (c["provider"], c["upstream_model"]) == NEGATIVE]
    head_raw = subprocess.run(  # noqa: S603  # nosec B603  # literal argv, reads the pre-wave commit
        ["git", "show", f"HEAD:{EVIDENCE_ROOT}/claims.merged.json"],  # noqa: S607  # nosec B607
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
        timeout=120,
    )
    head_claims = json.loads(head_raw.stdout)["claims"]
    head = [c for c in head_claims if (c["provider"], c["upstream_model"]) == NEGATIVE]
    drift = sorted({p for a, b in zip(head, disk, strict=False) for p in _diff_paths(a, b)})
    allowed = {
        "executed_at_utc",
        "receipt.sha256",
        "pins.probe.0.after_sha256",
        "pins.probe.0.before_sha256",
        "pins.probe.1.after_sha256",
        "pins.probe.1.before_sha256",
        "pins.fixture.0.after_sha256",
        "pins.fixture.0.before_sha256",
        "pins.fixture.1.after_sha256",
        "pins.fixture.1.before_sha256",
    }
    print(
        f"other_kind_vs_head records={len(head)}/{len(disk)} changed_paths={drift}"
        f" unexpected={sorted(set(drift) - allowed)}"
    )
    arms.append(
        (
            "negative_control_other_kind",
            neg_passes == neg_runs
            and neg["seam_kind"] == "shared_http_client"
            and neg["carrier_has_invoked_face"] is False
            and repeat_a == repeat_b
            and repeat_c == repeat_d
            and not (set(drift) - allowed)
            and len(head) == len(disk) == 5,
        )
    )

    after = _c101_witness(marker)
    symdiff = sorted(set(before["lines"]) ^ set(after["lines"]))
    print(
        f"c101_witness_after files={after['tracked_files']}"
        f" porcelain_lines={after['porcelain_lines']} porcelain_sha={after['porcelain_sha']}"
        f" newer_than_marker={after['newer_than_marker']} newer_sample={after['newer_sample']}"
        f" git_rc={after['git_rc']} find_rc={after['find_rc']}"
    )
    print(
        f"c101_untouched before_sha={before['porcelain_sha']} after_sha={after['porcelain_sha']}"
        f" symdiff={symdiff} files_equal={before['tracked_files'] == after['tracked_files']}"
        f" c101_files_newer_than_marker={after['newer_than_marker']}"
        f" scratch_files_newer_than_marker={after['scratch_new_files']}"
        f" scratch_new_sample={after['scratch_new_sample']}"
    )
    print(
        f"leg_roots legs={len(leg_roots)} distinct={sorted(set(leg_roots))}"
        f" c101_root_seen={str(REPO_ROOT / EVIDENCE_ROOT) in leg_roots}"
        f" scratch_root_seen={leg_roots.count(str(LEGS_EVIDENCE_ROOT))}"
    )
    arms.append(
        (
            "legs_ran_on_scratch_root",
            scratch["outside_repo"]
            and scratch["digests_match"]
            and len(scratch["rows"]) == len(LEG_ROWS)
            and len(leg_roots) == 11
            and set(leg_roots) == {str(LEGS_EVIDENCE_ROOT)}
            and str(REPO_ROOT / EVIDENCE_ROOT) not in leg_roots
            and not str(LEGS_EVIDENCE_ROOT).startswith(str(REPO_ROOT)),
        )
    )
    arms.append(
        (
            "c101_receipts_captures_untouched",
            before["git_rc"] == 0
            and before["find_rc"] == 0
            and after["git_rc"] == 0
            and after["find_rc"] == 0
            and symdiff == []
            and before["porcelain_sha"] == after["porcelain_sha"]
            and before["porcelain_lines"] == after["porcelain_lines"]
            and before["tracked_files"] == after["tracked_files"]
            and after["newer_than_marker"] == 0
            and before["newer_than_marker"] == 0,
        )
    )

    passed = sum(1 for _, ok in arms if ok)
    failed = [name for name, ok in arms if not ok]
    print(
        f"seam_liveness_face arms={len(arms)} arms_passed={passed} arms_failed={len(failed)}"
        f" failed={failed} rows_probed={len(SURFACES) + 3}"
        f" phase_runs={total_runs + neg_runs + 2} phase_passes={total_passes + neg_passes}"
        f" identity_set={gen['identity_set_sha256']} PASS={not failed}"
    )
    rc = 0 if not failed else 1
    print(f"w32_seam_liveness_exit={rc}")
    return rc


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _sha_bytes(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _diff_paths(a: Any, b: Any, prefix: str = "") -> list[str]:  # noqa: ANN401
    out: list[str] = []
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b)):
            if key not in a or key not in b:
                out.append(f"{prefix}{key}.presence")
            else:
                out.extend(_diff_paths(a[key], b[key], f"{prefix}{key}."))
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append(f"{prefix}len")
        for index, (left, right) in enumerate(zip(a, b, strict=False)):
            out.extend(_diff_paths(left, right, f"{prefix}{index}."))
    elif a != b:
        out.append(prefix.rstrip("."))
    return out


if __name__ == "__main__":
    raise SystemExit(main())
