"""Run the wave-37 full-tree re-sign chain, one carrier per shipped command.

Why the whole tree is re-produced rather than just cboe: this wave edited
``opendata/data/domains.yaml`` (a new ``cboe_index_snapshots`` key) and two cboe witness files, and
``domains.yaml`` is pinned as a ``contract`` file inside every landed receipt. Re-producing only
cboe would leave the other providers stale on the same digest, so plane 1 could not rise at all.

The staleness is proved before it is repaired, not after. ``judge_pre_chain`` runs the shipped
judge over the *committed* merged claims while the edits are already in the tree; the carrier
therefore records what a stale tree actually reads as, with its own exit line, and the post-chain
judge run is the only source for the accepted count.

Each carrier carries its own exact argv and its own exit reading (``chain_<name>_exit=``), so no
face in this directory is attributed to a run that printed nothing. The provider loop does not
abort on a non-zero member: a refusal is a reading to be recorded and attributed, not a reason to
stop measuring.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import subprocess  # nosec B404  # house style: list argv child processes, shell never enabled
import sys
from pathlib import Path
from typing import Any

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
PY = "/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python"
BUNDLE = "docs/evidence/C141/wave37-cboe-index-snapshots-20261019"
RUNS = REPO / BUNDLE / "runs"
WITNESS_DIR = REPO / "scripts/quality/iter2_witnesses"
INVENTORY = "docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/模型级任务清单.csv"
MERGED = REPO / "docs/evidence/C101/claims.merged.json"
JUDGE_OUTPUT = "docs/evidence/C101/judge/claims.merged.report.json"
PRE_CHAIN_OUTPUT = f"{BUNDLE}/runs/judge_pre_chain.report.json"
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


def providers() -> list[str]:
    """Every provider that ships a witness module, in deterministic order."""
    return sorted(p.stem for p in WITNESS_DIR.glob("*.py") if p.stem != "__init__")


def stable_sha(path: Path) -> str:
    """Digest of one file's payload with the generation timestamp removed."""
    if not path.is_file():
        return "MISSING"
    document = json.loads(path.read_text(encoding="utf-8"))
    body = (
        {k: v for k, v in document.items() if k != "generated_at_utc"}
        if isinstance(document, dict)
        else document
    )
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:16]


def run(name: str, argv: list[str], note: str) -> int:
    """Execute one shipped command, write its carrier, return its exit code."""
    started = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    proc = subprocess.run(  # noqa: S603  # nosec B603  # argv is this driver's own shipped command
        [PY] + argv,
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
        env={"PYTHONPATH": str(REPO), "PATH": "/usr/bin:/bin:/usr/local/bin"},
    )
    carrier = RUNS / f"{name}.txt"
    lines = [
        f"# carrier for {name}",
        f"# argv: {' '.join([PY] + argv)}",
        f"# note: {note}",
        f"# started_utc: {started}",
        f"# exit: {proc.returncode}",
        f"# stdout_chars: {len(proc.stdout)} stderr_chars: {len(proc.stderr)}",
        "",
        proc.stdout.rstrip("\n"),
        "",
        "---- stderr ----",
        proc.stderr.rstrip("\n"),
        "",
        f"chain_{name}_exit={proc.returncode}",
        "",
    ]
    carrier.write_text("\n".join(lines), encoding="utf-8")
    return proc.returncode


def report_face(tag: str, relative: str) -> dict[str, Any]:
    """Read the judge's own summary rather than restating what the driver intended."""
    path = REPO / relative
    if not path.is_file():
        return {f"{tag}_judge_report": "MISSING"}
    summary = json.loads(path.read_text(encoding="utf-8"))["summary"]
    return {
        f"{tag}_identities": summary["identity_count"],
        f"{tag}_accepted": summary["offline_acceptance_count"],
        f"{tag}_claims": summary["claim_count"],
        f"{tag}_status_counts": summary["status_counts"],
    }


def main() -> int:
    """Pre-chain judge -> producer -> recorder -> merge (twice) -> judge."""
    RUNS.mkdir(parents=True, exist_ok=True)
    names = providers()
    print(f"providers_with_witness={len(names)}", flush=True)
    exits: dict[str, int] = {}
    exits["judge_pre_chain"] = run(
        "judge_pre_chain",
        [
            "scripts/quality/iter2_model_evidence.py",
            "--inventory",
            INVENTORY,
            "--claims",
            "docs/evidence/C101/claims.merged.json",
            "--output",
            PRE_CHAIN_OUTPUT,
        ],
        "stale-tree counterfact: committed claims against the edited tree",
    )
    pre = report_face("pre_chain", PRE_CHAIN_OUTPUT)
    print(json.dumps(pre, ensure_ascii=False), flush=True)
    merged_before = stable_sha(MERGED)
    for provider in names:
        shard = f"docs/evidence/C101/{provider}/claims.json"
        reviewed = f"docs/evidence/C101/{provider}/claims.reviewed.json"
        exits[f"producer_{provider}"] = run(
            f"producer_{provider}",
            [
                "scripts/quality/iter2_claims_producer.py",
                "--provider",
                provider,
                "--output",
                shard,
            ],
            f"five-phase producer for {provider}",
        )
        print(f"producer_{provider}_exit={exits[f'producer_{provider}']}", flush=True)
        exits[f"recorder_{provider}"] = run(
            f"recorder_{provider}",
            [
                "scripts/quality/iter2_primary_review_recorder.py",
                "--claims",
                shard,
                "--output",
                reviewed,
                "--provider",
                provider,
            ]
            + REVIEWER_ARGS,
            f"primary reviews over {provider}'s regenerated shard",
        )
        print(f"recorder_{provider}_exit={exits[f'recorder_{provider}']}", flush=True)
    exits["merge_take1"] = run(
        "merge_take1", ["scripts/quality/iter2_claims_merge.py"], "merge every reviewed shard"
    )
    take1_sha = stable_sha(MERGED)
    snapshot = RUNS / "merge_take1.stable.json"
    snapshot.write_text(
        json.dumps(
            {
                k: v
                for k, v in json.loads(MERGED.read_text(encoding="utf-8")).items()
                if k != "generated_at_utc"
            },
            sort_keys=True,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    exits["merge_take2"] = run(
        "merge_take2", ["scripts/quality/iter2_claims_merge.py"], "take 2 of 2, idempotency"
    )
    take2_sha = stable_sha(MERGED)
    exits["judge"] = run(
        "judge",
        [
            "scripts/quality/iter2_model_evidence.py",
            "--inventory",
            INVENTORY,
            "--claims",
            "docs/evidence/C101/claims.merged.json",
            "--output",
            JUDGE_OUTPUT,
        ],
        "plane-1 judge over the 350 pinned identities",
    )
    post = report_face("post_chain", JUDGE_OUTPUT)
    summary = {
        "providers_loop": len(names),
        "pre_chain_stale_reading": pre,
        "post_chain_reading": post,
        "merged_stable_sha_before": merged_before,
        "merged_stable_sha_take1": take1_sha,
        "merged_stable_sha_take2": take2_sha,
        "merge_idempotent_stable_payload": take1_sha == take2_sha,
        "producer_exits": {k: v for k, v in exits.items() if k.startswith("producer_")},
        "recorder_exits": {k: v for k, v in exits.items() if k.startswith("recorder_")},
        "nonzero_members": sorted(k for k, v in exits.items() if v != 0),
    }
    line = " ".join(f"{k}={v}" for k, v in summary.items())
    (RUNS / "chain_summary.txt").write_text(
        "# wave-37 re-sign chain summary (cboe::IndexSnapshots landed)\n"
        "# argv: "
        + " ".join([PY, str(REPO / BUNDLE / "probes" / "w39_run_chain.py")])
        + "\n\n"
        + json.dumps(summary, indent=2, ensure_ascii=False, default=str)
        + "\n\n"
        + line
        + f"\n\nw37_chain_exit={0 if not summary['nonzero_members'] else 1}\n",
        encoding="utf-8",
    )
    print(line, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
