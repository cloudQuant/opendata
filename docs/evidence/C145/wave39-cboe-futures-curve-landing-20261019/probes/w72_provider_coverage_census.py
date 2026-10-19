"""Per-provider coverage census: the objective says 32 providers, so count rows per provider.

The headline claim to audit is "32 OpenBB providers implemented in this repo and accepted item by
item".
That is two different quantities and only the second one is falsifiable from the artifacts:

* how many providers the pinned inventory names (350 rows over N providers);
* how many of those providers have at least one row that plane 1 accepts, i.e. a row whose five
phases
  were executed and whose counterfact carries a refusal.

A provider with implemented fetchers but zero accepted rows is *not* accepted, so printing only the
registry size would let an implementation count answer an acceptance question. Both are printed, and
the
arms check that the join is not silent and that the classifier can report a zero provider.

``--source head`` reads the committed judge report (safe while a signing chain rewrites the
worktree);
``--source worktree`` reads the current one. Running both and diffing is how the wave's own movement
on
the zero-coverage list is measured instead of narrated.
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess  # nosec B404  # house style: fixed argv child processes, no shell
import sys
from collections import Counter
from pathlib import Path
from typing import Any

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
GIT = "/usr/bin/git"
JUDGE_REL = "docs/evidence/C101/judge/claims.merged.report.json"
LEDGER = REPO / "docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/模型级任务清单.csv"
RUNS = REPO / "docs/evidence/C145/wave39-cboe-futures-curve-landing-20261019/runs"
CARRIER = RUNS / "w72_provider_coverage_census.txt"


def read_judge(source: str) -> dict[str, Any]:
    """The judge report from git or from the worktree."""
    if source == "head":
        proc = subprocess.run(  # nosec B603  # noqa: S603
            [GIT, "show", f"HEAD:{JUDGE_REL}"], cwd=REPO, capture_output=True, check=False
        )
        if proc.returncode != 0:
            raise SystemExit("no committed judge report at HEAD")
        doc: dict[str, Any] = json.loads(proc.stdout)
        return doc
    doc = json.loads((REPO / JUDGE_REL).read_text(encoding="utf-8"))
    return doc


def inventory_providers() -> dict[str, int]:
    """Provider to row count, straight from the pinned task ledger."""
    counts: Counter[str] = Counter()
    with LEDGER.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            counts[str(row["provider"])] += 1
    return dict(counts)


def main() -> int:
    """Print coverage per provider and the arms that keep the census honest."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default="head", choices=("head", "worktree"))
    parser.add_argument("--suffix", default="", help="carrier suffix for a second take")
    args = parser.parse_args()

    lines: list[str] = []
    arms: dict[str, bool] = {}
    judge = read_judge(args.source)
    models = judge.get("models") or []
    rows_by_provider = inventory_providers()
    accepted: Counter[str] = Counter()
    status_by_provider: dict[str, Counter[str]] = {}
    for model in models:
        provider = str(model["provider"])
        status = str(model["status"])
        status_by_provider.setdefault(provider, Counter())[status] += 1
        if status == "OFFLINE_ACCEPTED":
            accepted[provider] += 1

    zero = sorted(p for p in rows_by_provider if accepted.get(p, 0) == 0)
    total_accepted = sum(accepted.values())
    lines.append(
        f"source={args.source} providers_in_ledger={len(rows_by_provider)} "
        f"ledger_rows={sum(rows_by_provider.values())} judge_models={len(models)} "
        f"accepted={total_accepted} "
        f"identity_set={judge['inventory']['identity_set_sha256'][:16]}"
    )
    lines.append(
        f"providers_with_at_least_one_accepted_row={len(rows_by_provider) - len(zero)} "
        f"providers_with_zero_accepted_rows={len(zero)} zero={zero}"
    )
    lines.extend(
        f"provider {provider:22} rows={rows_by_provider[provider]:3} "
        f"accepted={accepted.get(provider, 0):3} statuses="
        f"{json.dumps(dict(status_by_provider.get(provider, {})), ensure_ascii=False)}"
        for provider in sorted(rows_by_provider)
    )

    # A zero is only a measurement if the classifier can also report one, and if the join held.
    forged = dict(accepted)
    forged.pop(next(iter(rows_by_provider)), None)
    forged_zero = sorted(p for p in rows_by_provider if forged.get(p, 0) == 0)
    judge_accepted = int(judge["summary"]["offline_acceptance_count"])
    lines.append(
        f"join_accepted_to_models={total_accepted == judge_accepted} "
        f"statuses_sum={sum(sum(c.values()) for c in status_by_provider.values())} "
        f"control_can_report_a_zero_provider={len(forged_zero) > len(zero)}"
    )
    arms["A_join_matches_the_judges_own_count"] = total_accepted == int(
        judge["summary"]["offline_acceptance_count"]
    )
    arms["B_statuses_sum_to_the_inventory"] = sum(
        sum(c.values()) for c in status_by_provider.values()
    ) == sum(rows_by_provider.values())
    arms["C_ledger_covers_thirty_two_providers"] = len(rows_by_provider) == 32
    arms["D_zero_classifier_can_bite"] = len(forged_zero) > len(zero)
    arms["E_provider_names_join_both_ways"] = set(status_by_provider) == set(rows_by_provider)

    passed = sum(1 for v in arms.values() if v)
    ok = passed == len(arms)
    lines.append(
        f"provider_coverage_census arms={len(arms)} arms_passed={passed} PASS={ok} "
        f"red={sorted(k for k, v in arms.items() if not v)}"
    )
    text = "\n".join(lines) + "\n"
    text = text + f"w72_provider_coverage_census_exit={0 if ok else 1}" + "\n"
    RUNS.mkdir(parents=True, exist_ok=True)
    out = CARRIER if not args.suffix else CARRIER.with_stem(f"{CARRIER.stem}.{args.suffix}")
    out.write_text(text, encoding="utf-8")
    print(text[-1400:], end="")
    print(f"w72_provider_coverage_census_exit={0 if ok else 1}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
