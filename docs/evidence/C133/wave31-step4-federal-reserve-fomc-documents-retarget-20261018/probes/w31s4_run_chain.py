r"""Run the wave-31 step-4 landing-and-verification chain for ``federal_reserve::FomcDocuments``.

The landing is one retarget plus one witness entry, so what has to be priced is the whole
generation it produces: the five phases of the new row, the twelve rows that were already accepted
and get re-produced beside it, the merge and the plane-1 judge over all 350 pinned identities.
Every leg is a shipped command run as its own subprocess with list argv and no shell, and every
carrier in ``runs/`` carries its own exact argv, its own ``stderr_bytes`` and its own
``<name>_exit=<digits>`` line, so no face here rests on a summary.

Deliberately NOT in this roster: this wave's two pre-landing instruments
(``fomc_documents_retarget_prevalidation.py`` and ``fomc_retarget_cost_face.py``). Each asserts a
property of the PRE-retarget tree -- the first that the fetcher emits the
``federal_reserve_macro`` look-alike and that the row reads ``INCOMPLETE``, the second that the
shipped fetcher declares no ``row_model`` -- so after the landing they fail by construction. Their
readings are kept as the before-face and the third carrier here
(``handoff_judge_counterfact.py``) is the one that prices the state the landing leaves behind.

The reviewed-identity roster is read off the shard this chain produces, never typed here: a typed
list would assert which rows the producer happened to witness, which is exactly the fact under test.

Offline by construction: sockets are blocked inside every runner leg, ``OPENDATA_ALLOW_LIVE_E2E`` is
never set, and nothing in this file writes outside ``runs/`` and the shard paths the shipped
producer itself names.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess  # nosec B404  # the chain's shipped commands, list argv, shell never enabled
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
PY = "/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python"
STEP4 = "docs/evidence/C133/wave31-step4-federal-reserve-fomc-documents-retarget-20261018"
PROBES = REPO / STEP4 / "probes"
RUNS = REPO / STEP4 / "runs"
W27 = REPO / "docs/evidence/C128/plane1-residue-readiness-20261017/probes"
INVENTORY = "docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/模型级任务清单.csv"

PROVIDER = "federal_reserve"
LANDED = "FomcDocuments"
SHARD = f"docs/evidence/C101/{PROVIDER}/claims.json"
REVIEWED = f"docs/evidence/C101/{PROVIDER}/claims.reviewed.json"
REPORT = "docs/evidence/C101/judge/claims.merged.report.json"

WITNESS = "scripts/quality/iter2_witnesses/federal_reserve.py"
SHARD_PART = "scripts/quality/iter2_witnesses/_parts/federal_reserve/rows_a.py"
FETCHER = "opendata/data/providers/federal_reserve/models/fomc_documents.py"
HANDOFF_TEST = "tests/test_federal_reserve_iter2_models.py"

PRODUCER = [
    "scripts/quality/iter2_claims_producer.py",
    "--provider",
    PROVIDER,
    "--output",
    SHARD,
]
REVIEWER_ARGS = [
    "--decision",
    "ACCEPT",
    "--reviewer-type",
    "primary_model",
    "--reviewer-id",
    "iter2-acceptance-reviewer",
    "--reviewer-model",
    "qoder-cli-agent",
    "--reviewer-role",
    "acceptance_reviewer",
]
RECORD = ["scripts/quality/iter2_primary_review_recorder.py"]
MERGE = ["scripts/quality/iter2_claims_merge.py"]
JUDGE = [
    "scripts/quality/iter2_model_evidence.py",
    "--inventory",
    INVENTORY,
    "--claims",
    "docs/evidence/C101/claims.merged.json",
    "--output",
    REPORT,
]
LINT = ["-m", "ruff", "check", WITNESS, SHARD_PART, FETCHER, HANDOFF_TEST]
FORMAT = [
    "-m",
    "ruff",
    "format",
    "--check",
    WITNESS,
    SHARD_PART,
    FETCHER,
    HANDOFF_TEST,
    str(PROBES),
]
# The shipped typecheck plane is ``mypy $(PY_MYPY)`` = ``opendata alembic alembic_data``
# (Makefile:26), so tests/ is not mypy's population and neither is scripts/. Naming the
# re-based handoff test here would have priced 22 pre-existing errors in a file the gate
# never typechecks -- red invented by this chain's roster, not by the landing. The roster
# is therefore the two files the plane actually covers semantics for: the landed fetcher
# (in ``opendata``) and the witness module the claim pins.
MYPY = ["-m", "mypy", FETCHER, WITNESS]
BANDIT = ["-m", "bandit", "-q", "-c", "bandit.yaml", WITNESS, SHARD_PART, FETCHER]
TEST_FILES = [
    HANDOFF_TEST,
    "tests/test_federal_reserve_clean_documents.py",
    "tests/test_provider_model_contracts.py",
    "tests/test_provider_domains.py",
    "tests/test_iter2_model_evidence.py",
]
PYTEST = ["-m", "pytest", *TEST_FILES, "-o", "addopts=", "-p", "no:cacheprovider", "-q"]
COLLECT = [
    "-m",
    "pytest",
    *TEST_FILES,
    "-o",
    "addopts=",
    "-p",
    "no:cacheprovider",
    "--collect-only",
]


def run(name: str, argv: list[str], *, pythonpath: bool, note: str) -> dict[str, Any]:
    """Execute one shipped command and return its face (argv, exit, streams)."""
    proc = subprocess.run(  # noqa: S603  # nosec B603  # argv is this probe's own shipped command
        [PY] + argv,
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": "." if pythonpath else "", "PYTHONDONTWRITEBYTECODE": "1"},
    )
    print(
        f"{name}_exit={proc.returncode} stderr_bytes={len(proc.stderr.encode())} "
        f"stdout_lines={len(proc.stdout.splitlines())}",
        flush=True,
    )
    return {
        "name": name,
        "argv": [PY] + argv,
        "pythonpath": "." if pythonpath else None,
        "exit": proc.returncode,
        "stderr": proc.stderr,
        "stdout": proc.stdout,
        "note": note,
    }


def carrier(face: dict[str, Any], extra: list[str] | None = None) -> str:
    """Render one command face as the carrier text this wave commits."""
    lines = [
        f"wave-31 step 4 -- {face['note']}",
        f"argv: {' '.join(str(a) for a in face['argv'])}   "
        f"(cwd = repo root, no pipes, PYTHONPATH={face['pythonpath'] or 'not set'})",
        f"stderr_bytes={len(str(face['stderr']).encode())}",
    ]
    lines.extend(str(face["stdout"]).splitlines())
    if face["stderr"]:
        lines.append(f"stderr_first_line={str(face['stderr']).splitlines()[0]}")
    if extra:
        lines.extend(extra)
    lines.append(f"{face['name']}_exit={face['exit']}")
    return "\n".join(lines) + "\n"


def _norm(node: Any) -> Any:  # noqa: ANN401
    if isinstance(node, dict):
        return {
            k: _norm(v)
            for k, v in sorted(node.items())
            if k
            not in {
                "generated_at_utc",
                "executed_at_utc",
                "receipt",
                "path",
                "started_at",
                "finished_at",
            }
        }
    if isinstance(node, list):
        return [_norm(v) for v in node]
    return node


def stable_sha(path: Path) -> str:
    """Digest of a claims document ignoring the fields a re-run necessarily rotates."""
    blob = json.dumps(
        _norm(json.loads(path.read_text(encoding="utf-8"))), sort_keys=True, ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def pin_drift_face() -> dict[str, Any]:
    """Measure, per row, which pinned digests the landing has already staled."""
    claims = json.loads((REPO / SHARD).read_text(encoding="utf-8"))
    disk: dict[str, str] = {}
    per_row: dict[str, dict[str, set[str]]] = {}
    for claim in claims["claims"]:
        row = str(claim["upstream_model"])
        bucket = per_row.setdefault(row, {})
        for group in ("source", "contract", "fixture", "probe"):
            # ``contract_version`` is a bare digest string with no path, so it is not a file pin and
            # has no disk bytes to compare; the four groups walked here are the dict-shaped ones.
            for pin in claim["pins"].get(group) or []:
                rel = str(pin["path"])
                bucket.setdefault(rel, set()).add(str(pin["after_sha256"])[:16])
                if rel not in disk:
                    disk[rel] = hashlib.sha256((REPO / rel).read_bytes()).hexdigest()[:16]
    all_paths = sorted({rel for bucket in per_row.values() for rel in bucket})
    lines = [
        f"claims_document generated_at_utc={claims['generated_at_utc']} "
        f"identity_set={claims['identity_set_sha256'][:16]} claims={len(claims['claims'])}"
    ]
    for rel in all_paths:
        seen: set[str] = set()
        for bucket in per_row.values():
            seen |= bucket.get(rel, set())
        if disk[rel] not in seen:
            lines.append(f"STALE {rel} pinned={'/'.join(sorted(seen))} disk={disk[rel]}")
    drifted = sorted(
        row
        for row, bucket in per_row.items()
        if any(disk[rel] not in digests for rel, digests in bucket.items())
    )
    rows_with_pins = sorted(row for row, bucket in per_row.items() if bucket)
    rows_without_pins = sorted(row for row, bucket in per_row.items() if not bucket)
    stale_files = sum(1 for line in lines if line.startswith("STALE"))
    lines.append(
        f"pinned_files={len(all_paths)} stale_files={stale_files} "
        f"rows_total={len(per_row)} rows_with_pins={len(rows_with_pins)} "
        f"rows_without_pins={rows_without_pins or '[]'} drifted_rows={drifted or '[]'} "
        f"this_steps_row={LANDED}"
    )
    return {
        "name": "pin_drift_pre_production",
        "argv": [PY, str(PROBES / "w31s4_run_chain.py"), "--stage", "pin-drift"],
        "pythonpath": None,
        "exit": 0,
        "stderr": "",
        "stdout": "\n".join(lines),
        "note": "pinned digests of the shard on disk BEFORE this chain produced it, read over the "
        "shipped claims document exactly as the merge reads it. This argv is re-runnable and writes "
        "beside this file: taken before the landing it names the files the retarget has already "
        "changed, and after the chain has run it should read stale_files=0 -- which is the "
        "agreement this chain was asked to bring about, not a check that passed by being re-run",
    }


def judged_statuses() -> dict[str, str]:
    """Read the merged judge report's per-identity status map, or an empty dict before it exists."""
    path = REPO / REPORT
    if not path.is_file():
        return {}
    report = json.loads(path.read_text(encoding="utf-8"))
    return {str(row["model_identity"]): str(row["status"]) for row in report["models"]}


def report_face(tag: str) -> list[str]:
    """Print the plane-1 denominators, and federal_reserve's own buckets, under one tag."""
    path = REPO / REPORT
    if not path.is_file():
        return [f"{tag} report_absent={REPORT}"]
    report = json.loads(path.read_text(encoding="utf-8"))
    summary = report["summary"]
    rows: dict[str, list[str]] = {}
    for row in report["models"]:
        if row["provider"] == PROVIDER:
            rows.setdefault(str(row["status"]), []).append(str(row["upstream_model"]))
    lines = [
        f"{tag} identity_set={report['inventory']['expected_identity_set_sha256'][:16]} "
        f"identity_count={summary['identity_count']} "
        f"offline_acceptance_count={summary['offline_acceptance_count']} "
        f"claim_count={summary['claim_count']} "
        f"primary_review_count={summary['primary_review_count']} "
        f"status_counts={summary['status_counts']}"
    ]
    lines.extend(
        f"{tag} federal_reserve_{status}={len(rows[status])} {sorted(rows[status])}"
        for status in sorted(rows)
    )
    lines.append(
        f"{tag} this_steps_row_accepted={sorted(set(rows.get('OFFLINE_ACCEPTED', [])) & {LANDED})}"
    )
    lines.append(
        f"{tag} this_steps_row_judged={judged_statuses().get(f'{PROVIDER}::{LANDED}', 'ABSENT')}"
    )
    return lines


def witnessed_models() -> list[str]:
    """Derive the identities to review from the shard the producer just wrote: fully PASS rows."""
    claims = json.loads((REPO / SHARD).read_text(encoding="utf-8"))
    by_model: dict[str, Counter] = {}
    for claim in claims["claims"]:
        by_model.setdefault(str(claim["upstream_model"]), Counter())[str(claim["status"])] += 1
    passed = sorted(m for m, c in by_model.items() if c["PASS"] == 5 and sum(c.values()) == 5)
    blocked = sorted(m for m, c in by_model.items() if m not in passed)
    print(
        f"witness_roster derived={len(passed)} not_fully_passed={len(blocked)} {blocked}",
        flush=True,
    )
    return passed


def retake(leg: str) -> int:
    """Re-run one leg from ``VERIFICATION_LEGS`` and write it beside its first take."""
    matches = [item for item in VERIFICATION_LEGS if item[0] == leg]
    if len(matches) != 1:
        print(f"retake_leg_not_found name={leg} known={[i[0] for i in VERIFICATION_LEGS]}")
        return 2
    name, leg_argv, pypath, note = matches[0]
    face = run(name, leg_argv, pythonpath=pypath, note=note)
    path = RUNS / f"wave31-step4-{name}_retake.txt"
    path.write_text(carrier(face), encoding="utf-8")
    print(f"retake_carrier_written name={name} bytes={path.stat().st_size}", flush=True)
    return face["exit"]


VERIFICATION_LEGS: list[tuple[str, list[str], bool, str]] = [
    (
        "handoff_judge_counterfact",
        [str(PROBES / "handoff_judge_counterfact.py")],
        True,
        "the re-based handoff judge priced by forging each failure it must have: look-alike "
        "restored, semantic drift, population pin, retired comparison, restore",
    ),
    (
        "incomplete_row_guard_check",
        [str(W27 / "incomplete_row_guard_check.py")],
        True,
        "the four-axis residue census re-taken over the new residue population. Landing this row "
        "moves it out of INCOMPLETE entirely, so the histogram this carrier prints is the one that "
        "shows what ROW_CLASS_NOT_CONTRACT became; the instrument asserts nothing about this row's "
        "old label",
    ),
    (
        "readiness_census",
        [str(W27 / "readiness_census.py")],
        True,
        "five-column readiness census after the federal_reserve one-row landing",
    ),
    (
        "residue_axis_census",
        [str(W27 / "residue_axis_census.py")],
        False,
        "residue axis census with the eight arms, after the federal_reserve one-row landing",
    ),
    (
        "seam_reachability_census",
        [str(W27 / "seam_reachability_census.py")],
        False,
        "seam reachability re-pricing against the two shipped mechanisms",
    ),
    (
        "receipt_pin_consistency",
        [str(W27 / "receipt_pin_consistency.py")],
        True,
        "receipt pin plane over every claims receipt in the tree",
    ),
    (
        "batch5_fence_dry_census",
        [str(REPO / "docs/evidence/C103/batch5-source-pins/probes/pin_rebase.py")],
        False,
        "batch5 source-pin fence, dry: --apply deliberately NOT passed, so this run can only read. "
        "exit 1 on this argv is the designed refusal when stale=0, not a red check",
    ),
    (
        "ruff_check",
        LINT,
        False,
        "ruff over the witness module, its shard part, the landed fetcher "
        "and the rebased handoff test",
    ),
    ("ruff_format", FORMAT, False, "format check over the same four files plus this wave's probes"),
    (
        "mypy",
        MYPY,
        False,
        "mypy over the two files the shipped typecheck plane covers: the landed fetcher and the "
        "witness module (the handoff test is off this roster -- see the note on MYPY)",
    ),
    ("bandit", BANDIT, False, "bandit with the shipped bandit.yaml over the three landed files"),
    (
        "pytest_federal_reserve_and_pin_planes",
        PYTEST,
        True,
        "federal_reserve provider suites, contract-model, provider-domain and judge suites",
    ),
    (
        "pytest_collect_denominator",
        COLLECT,
        True,
        "the same five files collected, not run: passed plus skipped must equal collected, so no "
        "sample disappeared into a skip",
    ),
]


def main(argv: list[str]) -> int:
    """Run the chain and write every carrier into the wave's runs directory."""
    RUNS.mkdir(parents=True, exist_ok=True)
    if "--stage" in argv and argv[argv.index("--stage") + 1] == "pin-drift":
        drift = pin_drift_face()
        path = RUNS / "wave31-step4-pin_drift_pre_production_rerun.txt"
        path.write_text(carrier(drift), encoding="utf-8")
        print(f"rerun_carrier_written bytes={path.stat().st_size}", flush=True)
        return 0
    if "--stage" in argv and argv[argv.index("--stage") + 1] == "retake":
        return retake(argv[argv.index("--leg") + 1])

    # Each carrier is written the moment its own face is taken, not batched to the end: a crash in a
    # later leg then costs that leg's face only, instead of every face the chain had already read.
    written: list[str] = []

    def emit(face: dict[str, Any], extra: list[str] | None = None) -> None:
        path = RUNS / f"wave31-step4-{face['name']}.txt"
        path.write_text(carrier(face, extra), encoding="utf-8")
        written.append(face["name"])
        print(f"carrier_written name={face['name']} bytes={path.stat().st_size}", flush=True)

    drift = pin_drift_face()
    before = report_face("pre_chain")
    emit(drift, before)

    prod = run(
        "producer",
        PRODUCER,
        pythonpath=True,
        note="five-phase producer for all federal_reserve claims over its 13 inventory rows, with "
        "FomcDocuments witnessed through the _iter2_client seam instead of blocked",
    )
    emit(prod)

    models = witnessed_models()
    rec_argv = (
        RECORD
        + ["--claims", SHARD, "--output", REVIEWED]
        + sum([["--identity", f"{PROVIDER}::{m}"] for m in models], [])
        + REVIEWER_ARGS
    )
    rec = run(
        "recorder",
        rec_argv,
        pythonpath=True,
        note=f"primary reviews over the {len(models)} federal_reserve rows whose five claims all "
        "PASS in the regenerated shard (the roster is read off those claims, not typed here)",
    )
    emit(rec)

    merged = REPO / "docs/evidence/C101/claims.merged.json"
    m1 = run(
        "merge_take1",
        MERGE,
        pythonpath=True,
        note="claims merge over every reviewed shard, take 1 of 2",
    )
    emit(m1)
    take1_dir = Path(tempfile.mkdtemp())
    take1 = take1_dir / "claims.merged.take1.json"
    shutil.copy(merged, take1)
    m2 = run(
        "merge_take2",
        MERGE,
        pythonpath=True,
        note="claims merge, take 2 of 2 for the idempotency reading",
    )
    t1, t2 = stable_sha(take1), stable_sha(merged)
    emit(m2, [f"merge_idempotent_stable_payload: take1={t1} take2={t2} identical={t1 == t2}"])

    jd = run("judge", JUDGE, pythonpath=True, note="plane-1 judge over the 350 pinned identities")
    emit(jd, report_face("post_chain"))

    for name, leg_argv, pypath, note in VERIFICATION_LEGS:
        emit(run(name, leg_argv, pythonpath=pypath, note=note))

    print(f"carriers_written={len(written)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
