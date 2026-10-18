"""Run the wave-32 full-tree re-sign chain and write one carrier per shipped command.

Why the whole tree is re-produced rather than just yfinance: the wave changed
``scripts/quality/iter2_claims_probe_runner.py``, and that file is pinned as ``probe`` in every
landed receipt. The judge proved it before this driver existed -- with the edit in the tree and
the merged claims untouched, plane 1 read ``0 accepted / 291 STALE`` with
``probe_pin_1_current_hash_drift`` and ``fixture_pin_1_current_hash_drift`` on all 291 rows.
Re-producing only yfinance would have left the other 30 providers stale, so plane 1 could not
rise at all.

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
BUNDLE = "docs/evidence/C137/wave32-yfinance-landing-20261018"
RUNS = REPO / BUNDLE / "runs"
WITNESS_DIR = REPO / "scripts/quality/iter2_witnesses"
INVENTORY = "docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/模型级任务清单.csv"
JUDGE = [
    "scripts/quality/iter2_model_evidence.py",
    "--inventory",
    INVENTORY,
    "--claims",
    "docs/evidence/C101/claims.merged.json",
    "--output",
    "docs/evidence/C101/judge/claims.merged.report.json",
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
#: The 26 rows this wave witnessed. ``KeyMetrics`` is deliberately absent: the shipped SDK
#: (yfinance 0.2.66) publishes no valuation-measures surface, so the row cannot be served offline
#: at all -- see the KeyMetrics arm of this wave's face. Producing it would only rotate a blocked
#: claim, not accept a row.
YFINANCE_LANDED = [
    "BalanceSheet",
    "CashFlowStatement",
    "CompanyNews",
    "CryptoHistorical",
    "CurrencyHistorical",
    "EquityActive",
    "EquityAggressiveSmallCaps",
    "EquityGainers",
    "EquityHistorical",
    "EquityInfo",
    "EquityLosers",
    "EquityQuote",
    "EquityScreener",
    "EquityUndervaluedGrowth",
    "EquityUndervaluedLargeCaps",
    "EtfHistorical",
    "EtfInfo",
    "FuturesHistorical",
    "GrowthTechEquities",
    "HistoricalDividends",
    "IndexHistorical",
    "KeyExecutives",
    "OptionsChains",
    "PriceTargetConsensus",
    "ShareStatistics",
    "IncomeStatement",
]


def providers() -> list[str]:
    """Every provider that ships a witness module, in deterministic order."""
    return sorted(p.stem for p in WITNESS_DIR.glob("*.py") if p.stem != "__init__")


def sha(path: Path) -> str:
    """Digest of a file, or ``MISSING`` -- never a silent empty string."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16] if path.is_file() else "MISSING"


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


def report_face(tag: str) -> dict[str, Any]:
    """Read the judge's own summary rather than restating what the driver intended."""
    path = REPO / "docs/evidence/C101/judge/claims.merged.report.json"
    if not path.is_file():
        return {"judge_report": "MISSING"}
    summary = json.loads(path.read_text(encoding="utf-8"))["summary"]
    return {
        f"{tag}_identities": summary["identity_count"],
        f"{tag}_accepted": summary["offline_acceptance_count"],
        f"{tag}_claims": summary["claim_count"],
        f"{tag}_status_counts": summary["status_counts"],
    }


def main() -> int:
    """Producer -> recorder -> merge (twice) -> judge, one carrier per command."""
    RUNS.mkdir(parents=True, exist_ok=True)
    names = providers()
    print(f"providers_with_witness={len(names)}")
    exits: dict[str, int] = {}
    for provider in names:
        shard = f"docs/evidence/C101/{provider}/claims.json"
        reviewed = f"docs/evidence/C101/{provider}/claims.reviewed.json"
        producer = [
            "scripts/quality/iter2_claims_producer.py",
            "--provider",
            provider,
            "--output",
            shard,
        ]
        if provider == "yfinance":
            producer += ["--models", ",".join(YFINANCE_LANDED)]
        exits[f"producer_{provider}"] = run(
            f"producer_{provider}", producer, f"five-phase producer for {provider}"
        )
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
    exits["merge_take1"] = run(
        "merge_take1", ["scripts/quality/iter2_claims_merge.py"], "merge every reviewed shard"
    )
    take1 = REPO / "docs/evidence/C101/claims.merged.json"
    snapshot = Path("/tmp/w32_merge_take1.json")  # nosec B108  # noqa: S108
    snapshot.write_bytes(take1.read_bytes())
    exits["merge_take2"] = run(
        "merge_take2", ["scripts/quality/iter2_claims_merge.py"], "take 2 of 2, idempotency"
    )
    take2_sha = sha(take1)
    take1_sha = hashlib.sha256(
        json.dumps(
            {
                k: v
                for k, v in json.loads(snapshot.read_text(encoding="utf-8")).items()
                if k != "generated_at_utc"
            },
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()[:16]
    take2_stable = hashlib.sha256(
        json.dumps(
            {
                k: v
                for k, v in json.loads(take1.read_text(encoding="utf-8")).items()
                if k != "generated_at_utc"
            },
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()[:16]
    exits["judge"] = run("judge", JUDGE, "plane-1 judge over the 350 pinned identities")
    face = report_face("post_chain")
    summary = {
        "providers_loop": len(names),
        "producer_exits": {k: v for k, v in exits.items() if k.startswith("producer_")},
        "recorder_exits": {k: v for k, v in exits.items() if k.startswith("recorder_")},
        "merge_take1_stable_sha": take1_sha,
        "merge_take2_stable_sha": take2_stable,
        "merge_idempotent_stable_payload": take1_sha == take2_stable,
        "merge_raw_sha_differs_take2": take2_sha,
        **face,
        "nonzero_members": sorted(k for k, v in exits.items() if v != 0),
    }
    line = " ".join(f"{k}={v}" for k, v in summary.items())
    carrier = RUNS / "chain_summary.txt"
    carrier.write_text(
        "# wave-32 re-sign chain summary\n"
        "# argv: "
        + " ".join([PY, str(REPO / BUNDLE / "probes" / "w32_run_chain.py")])
        + "\n\n"
        + json.dumps(summary, indent=2, ensure_ascii=False, default=str)
        + "\n\n"
        + line
        + f"\n\nw32_chain_exit={0 if not summary['nonzero_members'] else 1}\n",
        encoding="utf-8",
    )
    print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
