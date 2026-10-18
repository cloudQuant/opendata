"""Compare wave 29's two carrier generations and prove the format pass changed no verdict.

The driver ``probes/resign_all_shards.py`` produced carrier step 2f at 07:23:55, then got
reformatted by ``ruff format`` at 07:30:38 — so the archived carrier and the script that is on
disk now are different generations, and "this script produced this carrier" had no carrier of its
own. The repair is not to rewrite the record: the four links (resign, merge, judge, verify) were
re-run from the current bytes as step 2f2/2g2/2h2, and this probe reads both generations of
carriers side by side. Every equality it asserts is a field read back out of an archived face line
or a per-provider ledger line, not a number transcribed by hand.

Arms:
* ``A``/``B``/``C`` - each pair of carriers must agree field by field (resign / merge+judge /
  verify).
* ``D`` - the report on disk hashes the merge on disk, and its accepted count equals step 2f2's
  ``signed_total``, so the archived chain and the product chain are one generation.
* ``E`` - the wave's landed face, aligned per identity against the **pinned pre-wave commit** (a
  full sha, because ``HEAD~n`` would silently retarget as soon as this wave is committed).
* ``F`` - falsification: rewrite the newer carrier's ``signed_total`` to 280, and pretend the
  pre-wave report already accepted one deribit row. A and E must each turn red; if they cannot,
  they are reading constants rather than files.
* ``G`` - after withdrawing both forgeries the same arms must be green again.
* ``H`` - the residue census that face.md section 6 and 8 publish: the ``UNREVIEWED`` population is
  exactly the set of providers with no claims shard on disk, the per-provider counts add up to the
  published bucket, and the largest cluster is yfinance (29), not tmx (24). The first draft of
  this wave's face claimed tmx was the largest cluster from a spot check; this arm is the census
  that refuted it, so it is pinned as a reading rather than as corrected prose.
* ``I`` - falsification of H by a *count-preserving* relabel: move yfinance's whole UNREVIEWED
  cluster onto wsj, so both totals stay at 61 and 350 while the two sets stop agreeing. If H stays
  green under that, H is reading the totals and not the partition.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[5]
RUNS = Path(__file__).resolve().parents[1] / "runs"
SHARD_REL_TMPL = "docs/evidence/C101/{}/claims.json"
sys.path.insert(0, str(REPO_ROOT / "scripts" / "quality"))

import iter2_model_evidence as judge  # noqa: E402  # the shipped CSV reader, not a paraphrase

BASE_COMMIT = "99834046c2db5eba1d39e9c8e176e0699a76a50c"  # 波 28 步骤 4 的提交 = 波 29 的波前
REPORT_REL = "docs/evidence/C101/judge/claims.merged.report.json"
MERGED_REL = "docs/evidence/C101/claims.merged.json"
EXPECTED_BASE_ACCEPTED = 276
EXPECTED_DISK_ACCEPTED = 281
EXPECTED_UNREVIEWED_BUCKET = 61
EXPECTED_INCOMPLETE_BUCKET = 8
EXPECTED_CSV_PROVIDERS = 32

RESIGN_FACE_RE = re.compile(r"^resign_face (?P<body>.*)$", re.M)
MERGE_FACE_RE = re.compile(
    r"^claims=(?P<claims>\d+) identities=(?P<identities>\d+) "
    r"reviews=(?P<reviews>\d+) accepted_identities=(?P<accepted>\d+)$",
    re.M,
)
JUDGE_FACE_RE = re.compile(r"^wrote .*\.md: (?P<face>.*)$", re.M)
VERIFY_FACE_RE = re.compile(r"^verify_merge_judge_face (?P<body>.*)$", re.M)
SIGN_LINE_RE = re.compile(r"^sign provider=(?P<p>\S+) (?P<body>.*)$", re.M)
ARM_LINE_RE = re.compile(r"^(?P<state>PASS|FAIL) (?P<arm>\S+)", re.M)


def read(name: str) -> str:
    """Read one archived carrier verbatim."""
    path = RUNS / name
    if not path.is_file():
        raise FileNotFoundError(f"missing carrier {path}")
    return path.read_text(encoding="utf-8")


def single(regex: re.Pattern[str], text: str, label: str) -> str:
    """Return the one face line a carrier holds; a wrong hit count is this probe's own bug."""
    hits = [m.group(0) for m in regex.finditer(text)]
    if len(hits) != 1:
        raise ValueError(f"{label}: expected 1 face line in carrier, found {len(hits)}")
    return hits[0]


def sign_ledger(text: str) -> dict[str, str]:
    """Per-provider signing ledger, keyed by provider so worker order cannot matter."""
    return {m.group("p"): m.group("body").strip() for m in SIGN_LINE_RE.finditer(text)}


def arm_states(text: str) -> dict[str, str]:
    """The per-arm verdict lines a verify carrier prints."""
    return {m.group("arm"): m.group("state") for m in ARM_LINE_RE.finditer(text)}


def status_index(doc: dict[str, Any]) -> dict[tuple[str, str], str]:
    """Map each identity to the status the judge published for it."""
    return {(str(m["provider"]), str(m["upstream_model"])): str(m["status"]) for m in doc["models"]}


def git_show(rel_path: str, commit: str = BASE_COMMIT) -> tuple[str, str]:
    """Read a file out of the pinned commit; never through a shell."""
    git = shutil.which("git")
    if git is None:
        return "", "git-not-found"
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [git, "show", f"{commit}:{rel_path}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return "", f"git-show-rc={proc.returncode}"
    return proc.stdout, ""


def sha16(data: bytes) -> str:
    """First 16 hex chars of the sha256 of these bytes."""
    return hashlib.sha256(data).hexdigest()[:16]


def compare_resign(old: str, new: str) -> tuple[bool, str]:
    """Arm A body: the two resign carriers must agree field by field."""
    face_old, face_new = (
        single(RESIGN_FACE_RE, old, "resign_face"),
        single(RESIGN_FACE_RE, new, "resign_face"),
    )
    ledger_old, ledger_new = sign_ledger(old), sign_ledger(new)
    ok = face_old == face_new and ledger_old == ledger_new
    diff = [
        f"{p}:{ledger_old[p]}!={ledger_new.get(p)}"
        for p in ledger_old
        if ledger_old[p] != ledger_new.get(p)
    ]
    return ok, (
        f"face_equal={face_old == face_new} ledger_old={len(ledger_old)} "
        f"ledger_new={len(ledger_new)} ledger_diffs={diff[:3]} "
        f"signed_total={'signed_total=281' in face_new}"
    )


def compare_merge_judge(old: str, new: str) -> tuple[bool, str]:
    """Arm B body: merge's face line and judge's face line must be identical across generations."""
    ok = single(MERGE_FACE_RE, old, "merge") == single(MERGE_FACE_RE, new, "merge") and single(
        JUDGE_FACE_RE, old, "judge"
    ) == single(JUDGE_FACE_RE, new, "judge")
    return ok, (
        f"merge_face={single(MERGE_FACE_RE, new, 'merge')!r} "
        f"judge_equal={single(JUDGE_FACE_RE, old, 'judge') == single(JUDGE_FACE_RE, new, 'judge')}"
    )


def compare_verify(old: str, new: str) -> tuple[bool, str]:
    """Arm C body: the seven per-arm verdicts and the verify face line must agree."""
    arms_old, arms_new = arm_states(old), arm_states(new)
    ok = arms_old == arms_new and single(VERIFY_FACE_RE, old, "verify") == single(
        VERIFY_FACE_RE, new, "verify"
    )
    return ok, (
        f"arms_old={arms_old} arms_new={arms_new} "
        f"face_new={single(VERIFY_FACE_RE, new, 'verify')!r}"
    )


def disk_is_the_archived_generation(disk_accepted: int, signed_total: int, digest_ok: bool) -> bool:
    """Arm D body: the disk report describes the disk merge, and both read the wave's number."""
    return digest_ok and disk_accepted == signed_total == EXPECTED_DISK_ACCEPTED


def transitions(old_doc: dict[str, Any], new_doc: dict[str, Any]) -> dict[str, Any]:
    """Per-identity status moves between two judge reports of the same population."""
    old_idx, new_idx = status_index(old_doc), status_index(new_doc)
    same_population = set(old_idx) == set(new_idx)
    moves = [
        (k, old_idx[k], new_idx[k]) for k in new_idx if same_population and old_idx[k] != new_idx[k]
    ]
    shapes: dict[str, int] = {}
    for _, before, after in moves:
        shapes[f"{before}->{after}"] = shapes.get(f"{before}->{after}", 0) + 1
    return {
        "same_population": same_population,
        "population": len(new_idx),
        "moves": moves,
        "shapes": shapes,
        "identity_set_equal": str(old_doc["inventory"]["identity_set_sha256"])[:16]
        == str(new_doc["inventory"]["identity_set_sha256"])[:16],
        "base_accepted": int(old_doc["summary"]["offline_acceptance_count"]),
        "disk_accepted": int(new_doc["summary"]["offline_acceptance_count"]),
    }


def wave29_shape(readings: dict[str, Any]) -> tuple[bool, str]:
    """Arm E body: exactly five deribit rows land and two government_us rows gain claims."""
    moves = readings["moves"]
    gained = sorted(k[0] for k, b, a in moves if a == "OFFLINE_ACCEPTED")
    incomplete = sorted(k[0] for k, b, a in moves if b == "UNREVIEWED" and a == "INCOMPLETE")
    ok = (
        readings["same_population"]
        and readings["identity_set_equal"]
        and readings["base_accepted"] == EXPECTED_BASE_ACCEPTED
        and readings["disk_accepted"] == EXPECTED_DISK_ACCEPTED
        and readings["shapes"] == {"UNREVIEWED->OFFLINE_ACCEPTED": 5, "UNREVIEWED->INCOMPLETE": 2}
        and gained == ["deribit"] * 5
        and incomplete == ["government_us", "government_us"]
    )
    return ok, (
        f"population={readings['population']} shapes={readings['shapes']} "
        f"gained={gained} incomplete={incomplete} "
        f"accepted={readings['base_accepted']}->{readings['disk_accepted']}"
    )


def inventory_providers() -> tuple[list[str], str]:
    """Read the provider column straight from the pinned inventory CSV.

    ``load_inventory`` is the shipped judge's own reader, so this is the same generation of the CSV
    that produced the report on disk -- and the CSV digest is returned so the caller can show that.
    ``os.chdir`` is needed because ``DEFAULT_INVENTORY`` is a repo-relative path.
    """
    previous = Path.cwd()
    os.chdir(REPO_ROOT)
    try:
        inventory = judge.load_inventory(judge.Path(judge.DEFAULT_INVENTORY))
    finally:
        os.chdir(previous)
    return sorted({str(row["provider"]) for row in inventory["rows"]}), str(inventory["csv_sha256"])


def unreviewed_partition(doc: dict[str, Any], providers: list[str]) -> dict[str, Any]:
    """Cut the no-claim population by provider and check it against the shard census on disk.

    ``providers`` is the inventory CSV's own provider column, read through the shipped judge. It
    must not be derived from ``doc``: the first draft of this arm did exactly that, and then the
    forgery in arm I -- which relabels rows -- silently removed a name from both sides of the
    comparison and still read as an agreement. A census whose denominator comes from the thing it
    counts cannot be broken, so it proves nothing.
    """
    counts = Counter(str(m["provider"]) for m in doc["models"] if str(m["status"]) == "UNREVIEWED")
    absent = [p for p in providers if not (REPO_ROOT / SHARD_REL_TMPL.format(p)).is_file()]
    bucket = int(doc["summary"]["status_counts"]["UNREVIEWED"])
    ordered = counts.most_common()
    return {
        "csv_providers": len(providers),
        "report_providers_equal_csv": sorted({str(m["provider"]) for m in doc["models"]})
        == providers,
        "shards_present": len(providers) - len(absent),
        "shards_absent": sorted(absent),
        "unreviewed_providers": sorted(counts),
        "counts": ordered,
        "partition_sum": sum(counts.values()),
        "bucket": bucket,
        "incomplete_bucket": int(doc["summary"]["status_counts"]["INCOMPLETE"]),
        "sets_equal": set(counts) == set(absent),
        "sum_matches_bucket": sum(counts.values()) == bucket,
    }


def arm_h(partition: dict[str, Any]) -> tuple[bool, str]:
    """Arm H body: UNREVIEWED is exactly the no-shard population, with its biggest cluster named."""
    counts = dict(partition["counts"])
    top3 = partition["counts"][:3]
    ok = (
        partition["sets_equal"]
        and partition["sum_matches_bucket"]
        and partition["report_providers_equal_csv"]
        and partition["bucket"] == EXPECTED_UNREVIEWED_BUCKET
        and partition["incomplete_bucket"] == EXPECTED_INCOMPLETE_BUCKET
        and partition["csv_providers"] == EXPECTED_CSV_PROVIDERS
        and partition["shards_present"] + len(partition["shards_absent"]) == EXPECTED_CSV_PROVIDERS
        and top3[0] == ("yfinance", 29)
        and counts["tmx"] == 24
    )
    return ok, (
        f"csv_providers={partition['csv_providers']} shards_present={partition['shards_present']} "
        f"shards_absent={partition['shards_absent']} "
        f"unreviewed_providers={partition['unreviewed_providers']} top3={top3} "
        f"partition_sum={partition['partition_sum']}==bucket={partition['bucket']} "
        f"sets_equal={partition['sets_equal']} incomplete_bucket={partition['incomplete_bucket']} "
        f"report_provider_set_matches_csv={partition['report_providers_equal_csv']}"
    )


def forge_partition(doc: dict[str, Any]) -> dict[str, Any]:
    """Tamper: relabel the whole yfinance UNREVIEWED cluster as wsj, holding both totals fixed.

    Moving one row would not do: yfinance would still appear in the counts and the two sets would
    still agree, so the "forgery" would only test the biggest-cluster clause. Relabeling all 29
    leaves the bucket at 61 and the population at 350 while yfinance drops out of the counts set —
    which is the only way this control can show that H reads the partition and not the totals.
    """
    forged = copy.deepcopy(doc)
    for model in forged["models"]:
        if model["provider"] == "yfinance" and model["status"] == "UNREVIEWED":
            model["provider"] = "wsj"
    return forged


def forge_resign_face(new: str) -> str:
    """Tamper: rewrite the archived signed_total to the value one fewer row would print."""
    return new.replace("signed_total=281", "signed_total=280", 1)


def forge_base_report(raw: str) -> str:
    """Tamper: pretend the previous generation already accepted one of the five deribit rows."""
    doc = json.loads(raw)
    for model in doc["models"]:
        if model["provider"] == "deribit" and model["status"] == "UNREVIEWED":
            model["status"] = "OFFLINE_ACCEPTED"
            break
    return json.dumps(doc)


def main() -> int:
    """Run the nine arms against the archived carriers and print one face line."""
    try:
        resign_old = read("wave29-step2f-resign_all_shards.txt")
        resign_new = read("wave29-step2f2-resign_all_shards_post_format.txt")
        mg_old = read("wave29-step2g-merge_and_judge.txt")
        mg_new = read("wave29-step2g2-merge_and_judge_post_format.txt")
        verify_old = read("wave29-step2h-verify_merge_judge_face.txt")
        verify_new = read("wave29-step2h2-verify_merge_judge_face_post_format.txt")

        report_bytes = (REPO_ROOT / REPORT_REL).read_bytes()
        merged_bytes = (REPO_ROOT / MERGED_REL).read_bytes()
        disk_report = json.loads(report_bytes)
        digest_ok = (
            str(disk_report["claims_input"]["sha256"]) == hashlib.sha256(merged_bytes).hexdigest()
        )
        signed_hit = re.search(r"\bsigned_total=(\d+)", resign_new)
        if signed_hit is None:
            raise ValueError("no signed_total on step 2f2's face line")
        signed_total = int(signed_hit.group(1))

        base_raw, base_err = git_show(REPORT_REL)
        base_report = json.loads(base_raw) if base_err == "" else None
        readings = transitions(base_report, disk_report) if base_report else {}

        a_ok, a_face = compare_resign(resign_old, resign_new)
        b_ok, b_face = compare_merge_judge(mg_old, mg_new)
        c_ok, c_face = compare_verify(verify_old, verify_new)
        d_ok = disk_is_the_archived_generation(
            int(disk_report["summary"]["offline_acceptance_count"]), signed_total, digest_ok
        )
        e_ok, e_face = (
            (False, f"base report unreadable: {base_err}")
            if not readings
            else wave29_shape(readings)
        )

        forged_resign = compare_resign(resign_old, forge_resign_face(resign_new))
        forged_base = (
            wave29_shape(transitions(json.loads(forge_base_report(base_raw)), disk_report))
            if base_report
            else (False, "forgery not measurable: base report unreadable")
        )
        f_ok = (not forged_resign[0]) and (not forged_base[0])
        restored = compare_resign(resign_old, resign_new)
        g_ok = restored[0] == a_ok and e_ok

        providers, csv_digest = inventory_providers()
        partition = unreviewed_partition(disk_report, providers)
        h_ok, h_face = arm_h(partition)
        forged_partition = unreviewed_partition(forge_partition(disk_report), providers)
        forged_h_ok, _ = arm_h(forged_partition)
        csv_generation_pinned = str(disk_report["inventory"]["csv_sha256"]) == csv_digest
        i_ok = (
            csv_generation_pinned
            and not forged_h_ok
            and not forged_partition["sets_equal"]
            and forged_partition["bucket"] == EXPECTED_UNREVIEWED_BUCKET
            and forged_partition["partition_sum"] == EXPECTED_UNREVIEWED_BUCKET
            and forged_partition["counts"][0][0] != "yfinance"
        )
        i_face = (
            f"csv_sha16_matches_report={csv_generation_pinned} "
            f"h_under_forgery={forged_h_ok}(want False) "
            f"sets_broke={not forged_partition['sets_equal']}(want True) "
            f"bucket_held={forged_partition['bucket']} "
            f"sum_held={forged_partition['partition_sum']} "
            f"rows_held={len(disk_report['models'])} "
            f"counts={forged_partition['counts'][:3]}"
        )

        arms = [
            ("A_resign_face_two_generations", a_ok, a_face),
            ("B_merge_judge_two_generations", b_ok, b_face),
            ("C_verify_arms_two_generations", c_ok, c_face),
            (
                "D_disk_is_the_archived_generation",
                d_ok,
                f"claims_input_digest_matches_disk={digest_ok} signed_total={signed_total} "
                f"disk_accepted={disk_report['summary']['offline_acceptance_count']}",
            ),
            ("E_wave29_delta_vs_pinned_commit", e_ok, e_face),
            (
                "F_forged_generations_go_red",
                f_ok,
                f"resign_tamper={forged_resign[0]}(want False) "
                f"base_tamper={forged_base[0]}(want False) tamper_detail={forged_base[1]}",
            ),
            ("G_restored_after_tamper", g_ok, f"resign_equal={restored[0]} e_still_green={e_ok}"),
            ("H_unreviewed_is_the_no_shard_population", h_ok, h_face),
            ("I_forged_partition_goes_red", i_ok, i_face),
        ]
        passed = 0
        for name, ok, face in arms:
            print(f"{'PASS' if ok else 'FAIL'} {name}")
            print(f"     {face}")
            passed += 1 if ok else 0
        print(
            f"post_format_two_generation_face arms={len(arms)} arms_passed={passed} "
            f"base={BASE_COMMIT[:12]} verdict={'GREEN' if passed == len(arms) else 'RED'}"
        )
        return 0 if passed == len(arms) else 1
    except Exception as exc:  # a carrier or a git read that vanished must not read as a green
        print(f"post_format_two_generation_face ERROR {type(exc).__name__}: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
