r"""Run the wave-31 step-3 landing-and-verification chain for government_us.

One carrier per command.

Each carrier carries its own exact argv and its own exit reading, so no face in this directory is
attributed to a run that printed nothing.

Why the whole government_us shard is re-produced for one row: the producer derives every receipt
path from ``--evidence-root`` and every pin from the files the witness module names, so editing
``opendata/data/providers/government_us/models/weather_bulletin_download.py`` and
``scripts/quality/iter2_witnesses/government_us.py`` rotates the source pin of every witnessed row
at once. Leaving the four already-landed rows on their previous digests would have the judge read
them as ``claim_receipt_hash_drift`` and plane 1 would fall before it rose. The pre-production
digest table is written as its own carrier so the delta is in the record, not in this prose.

Two things this driver inherits from its step-2 parent rather than reinventing: the drifted list is
derived per row from each claim's own pin table against the sha256 of the same bytes on disk -- not
printed as the list of models the wave set out to re-sign -- and ``rows_without_pins`` is measured
per row. That measurement has a sharper edge here, because the shard already carries the landed
row: in the pre-production table ``WeatherBulletinDownload`` is a BLOCKED row with zero pins, so it
belongs in ``rows_without_pins`` before the producer runs and in neither list after it. A row that
contradicts its own re-take is the finding, and the two carriers are kept side by side to show
which side of the chain each was taken on.

The verification legs live in ``VERIFICATION_LEGS`` so that ``--stage retake --leg <name>`` re-runs
exactly the command the chain ran, with the same note, instead of a second hand-written copy of it.
That is how the two carrier notes found to be unmeasured after the chain (see the wave's
``face.md``) get a re-taken carrier rather than an edited one.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess  # nosec B404  # the chain's shipped commands, list argv, shell never enabled
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
PY = "/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python"
STEP3 = "docs/evidence/C132/wave31-step3-government-us-weather-bulletin-download-20261018"
PROBES = REPO / STEP3 / "probes"
RUNS = REPO / STEP3 / "runs"
W27 = REPO / "docs/evidence/C128/plane1-residue-readiness-20261017/probes"
INVENTORY = "docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/模型级任务清单.csv"

MODELS = [
    "CommodityPsdData",
    "CommodityPsdReport",
    "TreasuryAuctions",
    "WeatherBulletin",
    "WeatherBulletinDownload",
]
NEWLY_LANDED = ["WeatherBulletinDownload"]
SHARD = "docs/evidence/C101/government_us/claims.json"
REVIEWED = "docs/evidence/C101/government_us/claims.reviewed.json"
REPORT = "docs/evidence/C101/judge/claims.merged.report.json"

WITNESS = "scripts/quality/iter2_witnesses/government_us.py"
MODELS_DIR = "opendata/data/providers/government_us/models"
LANDED = f"{MODELS_DIR}/weather_bulletin_download.py"

PRODUCER = [
    "scripts/quality/iter2_claims_producer.py",
    "--provider",
    "government_us",
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
LINT = ["-m", "ruff", "check", WITNESS, MODELS_DIR]
FORMAT = ["-m", "ruff", "format", "--check", WITNESS, LANDED, str(PROBES)]
MYPY = ["-m", "mypy", WITNESS, LANDED]
BANDIT = ["-m", "bandit", "-q", "-c", "bandit.yaml", WITNESS, LANDED]
TEST_FILES = [
    "tests/test_government_us_iter2.py",
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

VERIFICATION_LEGS: list[tuple[str, list[str], bool, str]] = [
    (
        "weather_bulletin_download_prevalidation",
        [str(PROBES / "weather_bulletin_download_prevalidation.py")],
        True,
        "25 phase legs under a scratch root, plus the five-served, declaration-removed "
        "(both shipped readers), media-type-membership, empty-body, multi-url-loop, "
        "query-pre-transport, one-cell, tamper-bites, doc-cites and static-versus-runtime arms",
    ),
    (
        "incomplete_row_guard_check",
        [str(W27 / "incomplete_row_guard_check.py")],
        True,
        "the wave-30 four-axis census re-taken over the residue population, with its six "
        "self-gating arms; this carrier prints the second-axis verdict histogram over the "
        "incomplete rows, while the action-map fall this landing was chosen for is printed by "
        "the residue_axis_census carrier of this same chain, not here",
    ),
    (
        "readiness_census",
        [str(W27 / "readiness_census.py")],
        True,
        "five-column readiness census after the government_us one-row landing",
    ),
    (
        "residue_axis_census",
        [str(W27 / "residue_axis_census.py")],
        False,
        "residue axis census with the eight arms, after the government_us one-row landing",
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
        "batch5 source-pin fence, dry: --apply deliberately NOT passed, so this run can only "
        "read. exit 1 on this argv is the designed refusal when stale=0, not a red check",
    ),
    ("ruff_check", LINT, False, "ruff over the witness module and the provider's model package"),
    (
        "ruff_format",
        FORMAT,
        False,
        "format check over the landed fetcher, the witness module and this wave's probes",
    ),
    ("mypy", MYPY, False, "mypy over the witness module and the landed fetcher"),
    ("bandit", BANDIT, False, "bandit with the shipped bandit.yaml over both edited files"),
    (
        "pytest_government_us_and_pin_planes",
        PYTEST,
        True,
        "government_us provider, contract-model, provider-domain and judge suites",
    ),
    (
        "pytest_collect_denominator",
        COLLECT,
        True,
        "the same four files collected, not run: passed plus skipped must equal collected, so no "
        "sample disappeared into a skip. This note replaces one asserting that the count moves "
        "when a model is added to the witness CONFIG -- never measured, and no file under tests/ "
        "imports the witness package",
    ),
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


def carrier(face: dict[str, Any], extra: list[str] | None = None) -> str:
    """Render one command face as the carrier text this wave commits."""
    lines = [
        f"wave-31 step 3 -- {face['note']}",
        f"argv: {' '.join(str(a) for a in face['argv'])}   "
        f"(cwd = repo root, no pipes, PYTHONPATH={face['pythonpath'] or 'not set'})",
        f"stderr_bytes={len(str(face['stderr']).encode())}",
    ]
    lines.extend(str(face["stdout"]).splitlines())
    if extra:
        lines.extend(extra)
    lines.append(f"{face['name']}_exit={face['exit']}")
    return "\n".join(lines) + "\n"


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
        f"this_steps_rows={NEWLY_LANDED}"
    )
    return {
        "name": "pin_drift_pre_production",
        "argv": [PY, str(PROBES / "w31s3_run_chain.py"), "--stage", "pin-drift"],
        "pythonpath": None,
        "exit": 0,
        "stderr": "",
        "stdout": "\n".join(lines),
        "note": (
            "government_us shard pins read straight out of "
            f"{SHARD} before any command in this chain ran, compared with the sha256 of the same "
            "bytes on disk, row by row; stale_files is the re-sign tax this landing charges. "
            "This argv is re-runnable and writes beside this file, and after the chain has run it "
            "reads stale_files=0 -- which is the agreement this chain was asked to bring about. "
            "rows_without_pins is measured per row, not the list of rows this step aimed to land: "
            "before the producer runs it carries the landed row, whose shard entry is a BLOCKED "
            "claim set with zero file pins, and after the chain it should carry only "
            "TreasuryPrices, the reserved row that binds no fetcher. A field that contradicts "
            "rows_with_pins in its own line is a label, not a reading"
        ),
    }


def report_face(tag: str) -> list[str]:
    """The plane-1 summary and the government_us row statuses, read out of the committed report."""
    report = json.loads((REPO / REPORT).read_text(encoding="utf-8"))
    summary = report["summary"]
    rows: dict[str, list[str]] = {}
    for row in report["models"]:
        if row["provider"] == "government_us":
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
        f"{tag} government_us_{status}={len(rows[status])} {sorted(rows[status])}"
        for status in sorted(rows)
        if status != "OFFLINE_ACCEPTED"
    )
    lines.append(f"{tag} government_us_offline_accepted={len(rows.get('OFFLINE_ACCEPTED', []))}")
    lines.append(
        f"{tag} this_steps_row_accepted="
        f"{sorted(set(rows.get('OFFLINE_ACCEPTED', [])) & set(NEWLY_LANDED))}"
    )
    return lines


def retake(leg: str) -> int:
    """Re-run one leg from ``VERIFICATION_LEGS`` and write it beside its first take."""
    matches = [item for item in VERIFICATION_LEGS if item[0] == leg]
    if len(matches) != 1:
        print(f"retake_leg_not_found name={leg} known={[i[0] for i in VERIFICATION_LEGS]}")
        return 2
    name, argv, pypath, note = matches[0]
    face = run(name, argv, pythonpath=pypath, note=note)
    path = RUNS / f"wave31-step3-{name}_retake.txt"
    path.write_text(carrier(face), encoding="utf-8")
    print(f"retake_carrier_written name={name} bytes={path.stat().st_size}", flush=True)
    return face["exit"]


def main(argv: list[str]) -> int:
    """Run the chain and write every carrier into the wave's runs directory."""
    RUNS.mkdir(parents=True, exist_ok=True)
    if "--stage" in argv and argv[argv.index("--stage") + 1] == "pin-drift":
        # The pre-production table can only be taken once, so a re-take writes beside it, not over
        # it, and prints the reading that tells the reader which side of the chain they are on.
        drift = pin_drift_face()
        path = RUNS / "wave31-step3-pin_drift_pre_production_rerun.txt"
        path.write_text(carrier(drift), encoding="utf-8")
        print(f"rerun_carrier_written bytes={path.stat().st_size}", flush=True)
        return 0
    if "--stage" in argv and argv[argv.index("--stage") + 1] == "retake":
        return retake(argv[argv.index("--leg") + 1])
    drift = pin_drift_face()
    before = report_face("pre_chain")
    faces: list[tuple[dict[str, Any], list[str] | None]] = [(drift, before)]

    prod = run(
        "producer",
        PRODUCER,
        pythonpath=True,
        note="five-phase producer for all 30 government_us claims over 6 inventory rows, "
        "with the landed row witnessed instead of blocked",
    )
    faces.append((prod, None))

    rec_argv = (
        RECORD
        + ["--claims", SHARD, "--output", REVIEWED]
        + sum([["--identity", f"government_us::{m}"] for m in MODELS], [])
        + REVIEWER_ARGS
    )
    rec = run(
        "recorder",
        rec_argv,
        pythonpath=True,
        note="primary reviews over the five witnessed government_us rows in the regenerated shard",
    )
    faces.append((rec, None))

    merged = REPO / "docs/evidence/C101/claims.merged.json"
    m1 = run(
        "merge_take1",
        MERGE,
        pythonpath=True,
        note="claims merge over every reviewed shard, take 1 of 2",
    )
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
    faces.append((m1, None))
    faces.append(
        (m2, [f"merge_idempotent_stable_payload: take1={t1} take2={t2} identical={t1 == t2}"])
    )

    jd = run("judge", JUDGE, pythonpath=True, note="plane-1 judge over the 350 pinned identities")
    faces.append((jd, report_face("post_chain")))

    for name, leg_argv, pypath, note in VERIFICATION_LEGS:
        faces.append((run(name, leg_argv, pythonpath=pypath, note=note), None))

    for face, extra in faces:
        path = RUNS / f"wave31-step3-{face['name']}.txt"
        path.write_text(carrier(face, extra), encoding="utf-8")
        print(f"carrier_written name={face['name']} bytes={path.stat().st_size}", flush=True)
    print(f"carriers_written={len(faces)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
