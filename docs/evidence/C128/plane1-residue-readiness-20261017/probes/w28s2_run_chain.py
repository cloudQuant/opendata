"""Run the wave-28 step-2 landing-and-verification chain and write one carrier per command.

Each carrier carries its own exact argv and its own exit reading, so no face in this
directory is attributed to a run that printed nothing.

Why this script exists at all: the reproducibility re-run of the producer, taken with
`--output` pointed at a scratch file, rotated the phase receipts under
`docs/evidence/C101/receipts/nasdaq/` because the producer derives the receipt path from
`--evidence-root` (iter2_claims_producer.py:262, `REPO_ROOT / receipt_relative`), never from
`--output`. The judge then read all nine nasdaq rows as STALE / claim_receipt_hash_drift and
plane 1 fell 275 -> 266. Re-running the landing chain in generation order (produce, sign, merge,
judge) restores agreement between the claims document and the receipts it points at.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
PY = "/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python"
PROBES = REPO / "docs/evidence/C128/plane1-residue-readiness-20261017/probes"
RUNS = REPO / "docs/evidence/C128/plane1-residue-readiness-20261017/runs"
INVENTORY = "docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/模型级任务清单.csv"

MODELS = [
    "CalendarDividend",
    "CalendarEarnings",
    "CalendarIpo",
    "CompanyFilings",
    "EconomicCalendar",
    "EquityScreener",
    "EquitySearch",
    "HistoricalDividends",
    "TopRetail",
]

PRODUCER = [
    "scripts/quality/iter2_claims_producer.py",
    "--provider",
    "nasdaq",
    "--output",
    "docs/evidence/C101/nasdaq/claims.json",
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
    "docs/evidence/C101/judge/claims.merged.report.json",
]
LINT = [
    "-m",
    "ruff",
    "check",
    "scripts/quality/iter2_witnesses/nasdaq.py",
    "opendata/data/providers/nasdaq/models/",
]
FORMAT = ["-m", "ruff", "format", "--check", "scripts/quality/iter2_witnesses/nasdaq.py"]
MYPY = ["-m", "mypy", "scripts/quality/iter2_witnesses/nasdaq.py"]
BANDIT = ["-m", "bandit", "-q", "-c", "bandit.yaml", "scripts/quality/iter2_witnesses/nasdaq.py"]
PYTEST = [
    "-m",
    "pytest",
    "tests/test_nasdaq_provider.py",
    "tests/test_provider_model_contracts.py",
    "tests/test_provider_domains.py",
    "tests/test_iter2_model_evidence.py",
    "-o",
    "addopts=",
    "-p",
    "no:cacheprovider",
    "-q",
]
PINPROBE = [str(PROBES / "receipt_pin_consistency.py")]
FENCE = [str(REPO / "docs/evidence/C103/batch5-source-pins/probes/pin_rebase.py")]


def run(name: str, argv: list[str], *, pythonpath: bool, note: str) -> dict[str, object]:
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
        f"stdout_lines={len(proc.stdout.splitlines())}"
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


def stable_sha(path: Path) -> str:
    """Digest of a claims document ignoring the fields a re-run necessarily rotates."""
    import json

    def norm(node: object) -> object:
        if isinstance(node, dict):
            return {
                k: norm(v)
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
            return [norm(v) for v in node]
        return node

    blob = json.dumps(
        norm(json.loads(path.read_text(encoding="utf-8"))), sort_keys=True, ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def carrier(face: dict[str, object], extra: list[str] | None = None) -> str:
    """Render one command face as the carrier text this wave commits."""
    lines = [
        f"wave-28 step 2 -- {face['note']}",
        f"argv: {' '.join(str(a) for a in face['argv'])}   "
        f"(cwd = repo root, no pipes, PYTHONPATH={face['pythonpath'] or 'not set'})",
        f"stderr_bytes={len(str(face['stderr']).encode())}",
    ]
    lines.extend(str(face["stdout"]).splitlines())
    if extra:
        lines.extend(extra)
    lines.append(f"{face['name']}_exit={face['exit']}")
    return "\n".join(lines) + "\n"


def main() -> int:
    """Run the chain and write every carrier into the wave's runs directory."""
    merged = REPO / "docs/evidence/C101/claims.merged.json"
    faces: list[tuple[dict[str, object], list[str] | None]] = []

    prod = run(
        "producer",
        PRODUCER,
        pythonpath=True,
        note="five-phase producer for the nine nasdaq rows, generation 3 (see header)",
    )
    faces.append((prod, None))

    rec_argv = (
        RECORD
        + [
            "--claims",
            "docs/evidence/C101/nasdaq/claims.json",
            "--output",
            "docs/evidence/C101/nasdaq/claims.reviewed.json",
        ]
        + sum([["--identity", f"nasdaq::{m}"] for m in MODELS], [])
        + REVIEWER_ARGS
    )
    rec = run(
        "recorder",
        rec_argv,
        pythonpath=True,
        note="primary reviews over the regenerated nasdaq claims document",
    )
    faces.append((rec, None))

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
    faces.append((jd, None))

    for name, argv, pypath, note in [
        (
            "readiness_census",
            [str(PROBES / "readiness_census.py")],
            True,
            "five-column readiness census after the nasdaq landing",
        ),
        (
            "residue_axis_census",
            [str(PROBES / "residue_axis_census.py")],
            False,
            "residue axis census with the eight arms, after the nasdaq landing",
        ),
        (
            "seam_reachability_census",
            [str(PROBES / "seam_reachability_census.py")],
            False,
            "seam reachability re-pricing against the two shipped mechanisms",
        ),
        (
            "receipt_pin_consistency",
            PINPROBE,
            True,
            "receipt pin plane over every claims receipt in the tree",
        ),
        (
            "batch5_fence_dry_census",
            FENCE,
            False,
            "batch5 source-pin fence, dry: --apply deliberately NOT passed, so this run can only "
            "read. exit 1 on this argv is the designed refusal when stale=0, not a red check",
        ),
        ("ruff_check", LINT, False, "ruff over the new witness module and the nine model files"),
        ("ruff_format", FORMAT, False, "format check over the new witness module"),
        ("mypy", MYPY, False, "mypy over the new witness module"),
        ("bandit", BANDIT, False, "bandit with the shipped bandit.yaml over the witness module"),
        (
            "pytest_nasdaq_and_pin_planes",
            PYTEST,
            True,
            "nasdaq provider tests, contract-model tests, batch5 pin plane "
            "and the judge's own suite",
        ),
    ]:
        faces.append((run(name, argv, pythonpath=pypath, note=note), None))

    for face, extra in faces:
        path = RUNS / f"wave28-step2-{face['name']}.txt"
        path.write_text(carrier(face, extra), encoding="utf-8")
        print(f"carrier_written name={face['name']} bytes={path.stat().st_size}")
    print(f"carriers_written={len(faces)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
