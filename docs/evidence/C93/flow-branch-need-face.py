#!/usr/bin/env python3
"""C93 flow.branch need-face: the five audited rows, judged before and after the repair.

C92's ladder put ``flow.branch`` at step 0 and credited it with unlocking five rows. This round
audited those five lines one by one against the pinned upstream models and found that each of them
needs at least one additional seam beyond the branch kernel -- 14 need spellings were added across
the five rows (3/4/2/4/1). Two of those spellings had no canonical to file under at all, so this
round registered ``shape.single_record_wrap`` and ``flow.nearest_published_value``. The claim this
archive owns is therefore narrow and falsifiable:

* building ``flow.branch`` would ship **zero** of the five audited rows;
* the credit moved because the need sets grew, not because a row was deleted or the population
  shifted -- the population face is identical in both arms;
* the two new canonicals read ``absent`` from the disk, and the device that says so can tell
  present from absent (it is not a dead instrument).

Arms, all offline, all through the repo's own judge (``model_capability_census.run``):

* ``ARM-VOCAB``     -- the two new entries' on-disk readings, plus the live ``ModelSpec`` field list
  showing neither declaration probe names a field that exists.
* ``ARM-DEVICE``    -- two-sided positive control on a *real* anchor: the same entries re-flagged
  ``read_from_disk`` must flip to ``present``, and the same entries with a forged anchor must flip
  back to ``absent``. A one-sided ``absent`` reading is not a measurement.
* ``ARM-LADDER``    -- pre-repair census bytes (``PRE_REPAIR_REV``) and the working-tree census
  bytes, same registry, same declaration scan: flow.branch's step, its unlocked ids, the audited
  five, which step now credits each of them, tradier's locked need set read against the registry
  statuses, and the ladder-composition identity ``entered == promoted`` (adding needs cannot
  retire a step).
* ``ARM-IDENTITY``  -- the label closure: sum of per-row added spellings == the mention delta the
  judge itself reports, and the canonical delta is measured against the pinned pre-repair ARTIFACT
  bytes, because the two census arms deliberately share one registry and so cannot differ there.
* ``ARM-REFUSAL``   -- two-sided, in memory: dropping the two spellings alone aborts on the judge's
  orphan guard (the registry still names capabilities no label maps to), and dropping the spellings
  *and* the registry entries aborts on the unmapped-label guard. Either way the repaired rows cannot
  quietly lose their new blockers; the restored run must read the same face as ARM-POST.

Exit 0 requires every arm to read as documented, including that the pre-repair bytes still
reproduce the 5-row credit. If they stop doing so, this file goes red rather than quietly agreeing.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess  # nosec B404  # read-only git argv, shell disabled
import sys
import tempfile
from pathlib import Path
from typing import Any, Final

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "quality"))
sys.path.insert(0, str(REPO_ROOT))

import model_capability_census as mcc  # noqa: E402

from opendata.data.providers._engine.spec import ModelSpec  # noqa: E402

CENSUS_REL: Final = mcc.CENSUS_DIR_REL
ROADMAP_REL: Final = mcc.ROADMAP_REL
# Pinned to the commit that carries C92's census bytes, not HEAD~1: the C93 commit moves HEAD, and a
# control whose "before" bytes drift into the "after" bytes would read the same face twice.
PRE_REPAIR_REV: Final = "dd11d42"
AUDITED: Final = frozenset(
    {
        "OBB2-federal_reserve-CentralBankHoldings",
        "OBB2-fred-FredRegional",
        "OBB2-tradier-EquitySearch",
        "OBB2-tradingeconomics-EconomicCalendar",
        "OBB2-cboe-IndexSnapshots",
    }
)
NEW_CAPS: Final = ("shape.single_record_wrap", "flow.nearest_published_value")
# The one audited row that leaves the greedy prefix: the two spellings this round added to it both
# map onto capabilities the engine does not have, so no single ladder step can unlock it.
TRADIER: Final = "OBB2-tradier-EquitySearch"
# The raw census spellings those two canonicals were registered for.
NEW_SPELLINGS: Final = ("single_object_array_wrap", "nearest_published_date_snap")
# A token that really is on disk: C92 shipped this field, so re-flagging an entry onto it is the
# side of the control that must read present.
CONTROL_ANCHOR: Final = "order_by: RowOrderSpec | None = None"
CONTROL_FILE: Final = mcc.SPEC_REL
FORGED_ANCHOR: Final = "wrap_single_record: Literal[True] = never_shipped"
# Equal across the two ladder arms on purpose: the ladder movement must be attributable to the need
# sets alone, so any change here is an edited population rather than a repaired row.
POPULATION_KEYS: Final = ("rows", "unique", "guard", "pending", "expressible", "files")


def _git(argv: list[str]) -> str:
    proc = subprocess.run(  # nosec B603  # noqa: S603
        argv,
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
        text=True,
    )
    return proc.stdout


def _materialize_pre_repair(rev: str, outdir: Path) -> int:
    """Write ``rev``'s census bytes into ``outdir`` so the judge can read them verbatim."""
    outdir.mkdir(parents=True, exist_ok=True)
    listing = _git(["git", "show", "--name-only", "--format=", f"{rev}:{CENSUS_REL}"]).splitlines()
    names = [
        n.rsplit("/", 1)[-1]
        for n in listing
        if n.rsplit("/", 1)[-1].startswith("census-") and n.endswith(".json")
    ]
    for name in names:
        blob = subprocess.run(  # nosec B603, B607  # noqa: S603
            ["git", "show", f"{rev}:{CENSUS_REL}/{name}"],  # noqa: S607
            cwd=REPO_ROOT,
            capture_output=True,
            check=True,
        ).stdout
        (outdir / name).write_bytes(blob)
    return len(names)


def _ladder_face(census_dir: Path | None) -> dict[str, Any]:
    """Run the repo's judge over one census directory and return the faces this archive reads."""
    if census_dir is None:
        report = mcc.run(round_id="C93-CONTROL")
    else:
        report = mcc.run(round_id="C93-CONTROL", census_dir=census_dir)
    face = json.loads(mcc.serialize(report))
    steps = face["roadmap"]["greedy_build_order"]
    branch_step = next((s for s in steps if s["capability"] == "flow.branch"), None)
    branch_ids = sorted(r["task_id"] for r in (branch_step or {}).get("newly_unlocked_rows", []))
    branch_index = next((i for i, s in enumerate(steps) if s["capability"] == "flow.branch"), -1)
    credited: dict[str, str] = {}
    for step in steps:
        for row in step["newly_unlocked_rows"]:
            credited.setdefault(row["task_id"], step["capability"])
    locked_rows = face["roadmap"]["rows_still_locked_after_engine_pool"]
    locked_needs = {
        r["task_id"]: sorted(r["needs"]) for r in locked_rows if r["task_id"] in AUDITED
    }
    statuses = {k: v["status"] for k, v in face["capability_registry"].items()}
    return {
        "round": face["round_id"],
        "rows": face["census"]["row_count_measured"],
        "unique": face["census"]["unique_task_ids"],
        "guard": face["census"]["row_count_guard_fired"],
        "files": len(face["census"]["files"]),
        "steps": len(steps),
        "step_caps": [s["capability"] for s in steps],
        "branch_index": branch_index,
        "branch_n": len(branch_ids),
        "branch_ids": branch_ids,
        "branch_audited": sorted(set(branch_ids) & set(AUDITED)),
        "pending": face["roadmap"]["rows_pending"],
        "expressible": face["roadmap"]["rows_expressible_today"],
        "shipped_cover": len(face["roadmap"]["shipped_cover_rows"]),
        "prefix": sorted(credited),
        "locked": sorted(r["task_id"] for r in locked_rows),
        "locked_needs": locked_needs,
        "statuses": statuses,
        "canonical": face["taxonomy"]["canonical_capability_count"],
        "labels_supported": face["taxonomy"]["labels_supported"],
        "raw_labels": face["taxonomy"]["raw_label_count"],
        "mentions": face["taxonomy"]["raw_label_mention_count"],
        "credited": credited,
        "registry": face["capability_registry"],
    }


def _added_spellings(pre_dir: Path) -> dict[str, int]:
    """Per audited row: how many raw spellings the repair added, read from the two byte states."""
    working = REPO_ROOT / CENSUS_REL
    out: dict[str, int] = {}
    for path in sorted(pre_dir.glob("census-*.json")):
        pre_doc = json.loads(path.read_text(encoding="utf-8"))
        pre_rows = {r["task_id"]: r for r in pre_doc.get("rows", [])}
        post_doc = json.loads((working / path.name).read_text(encoding="utf-8"))
        post_rows = {r["task_id"]: r for r in post_doc.get("rows", [])}
        for task_id in AUDITED:
            if task_id not in pre_rows:
                continue
            pre = set(pre_rows[task_id]["needs_engine_capability"])
            post = set(post_rows[task_id]["needs_engine_capability"])
            added = post - pre
            removed = pre - post
            if removed:
                raise SystemExit(
                    f"{task_id}: the repair removed {sorted(removed)}, expected additions only"
                )
            out[task_id] = len(added)
    return out


def main() -> int:
    """Run every arm, print its reading, and fail if any of them disagrees."""
    failures: list[str] = []

    resolved = _git(["git", "rev-parse", "--short", PRE_REPAIR_REV]).strip()
    head = _git(["git", "rev-parse", "--short", "HEAD"]).strip()
    print(f"PIN pre-repair rev={PRE_REPAIR_REV} resolves_to={resolved} (HEAD={head})")

    declarations = mcc.count_declarations(mcc.REGISTRY, REPO_ROOT)[0]
    readings = mcc.verify_registry(mcc.REGISTRY, REPO_ROOT, declarations)
    for cap in sorted(NEW_CAPS):
        reading = readings[cap]
        decls = reading.production_declarations
        print(
            f"ARM-VOCAB {cap}: status={reading.status} anchor_found={reading.anchor_found} "
            f"anchor_line={reading.anchor_line_number} claim={reading.claim} "
            f"read_from_disk={reading.read_from_disk} declarations={decls}"
        )
        if reading.status != "absent" or not reading.anchor_found:
            failures.append(f"{cap} should read absent on a found anchor")
        if decls != 0:
            failures.append(f"{cap} reads a live declaration; the vocabulary claim is absent")

    fields = sorted(f.name for f in dataclasses.fields(ModelSpec))
    probes = {
        e.probe.keyword for e in mcc.REGISTRY if e.capability in NEW_CAPS and e.probe is not None
    }
    names_a_field = any(p in fields for p in probes)
    print(
        f"ARM-VOCAB ModelSpec fields n={len(fields)} probe_keywords={sorted(probes)} "
        f"probe_names_a_field={names_a_field}"
    )
    if names_a_field:
        failures.append("a declaration probe names a real ModelSpec field, so absent cannot hold")

    by_cap = {e.capability: e for e in mcc.REGISTRY}
    on = mcc.verify_registry(
        tuple(
            dataclasses.replace(
                by_cap[c], read_from_disk=True, proof_file=CONTROL_FILE, anchor=CONTROL_ANCHOR
            )
            for c in NEW_CAPS
        ),
        REPO_ROOT,
        declarations,
    )
    forged = mcc.verify_registry(
        tuple(
            dataclasses.replace(
                by_cap[c], read_from_disk=True, proof_file=CONTROL_FILE, anchor=FORGED_ANCHOR
            )
            for c in NEW_CAPS
        ),
        REPO_ROOT,
        declarations,
    )
    print(
        f"ARM-DEVICE real-anchor={[on[c].status for c in NEW_CAPS]} "
        f"forged-anchor={[forged[c].status for c in NEW_CAPS]}"
    )
    if not all(on[c].status == "present" for c in NEW_CAPS):
        failures.append(
            "the device cannot read present off a real anchor: it is dead, not measuring"
        )
    if not all(forged[c].status == "absent" for c in NEW_CAPS):
        failures.append(
            "a forged anchor still reads present: the reading is assertable, not measured"
        )

    with tempfile.TemporaryDirectory(prefix="c93_census_control_") as raw:
        pre_dir = Path(raw) / "pre"
        pre_files = _materialize_pre_repair(PRE_REPAIR_REV, pre_dir)
        pre = _ladder_face(pre_dir)
        added = _added_spellings(pre_dir)
    print(f"ARM-LADDER materialized census files={pre_files} from {PRE_REPAIR_REV}")
    post = _ladder_face(None)

    for tag, face in (("ARM-PRE ", pre), ("ARM-POST", post)):
        print(
            f"{tag} rows={face['rows']} unique={face['unique']} guard={face['guard']} "
            f"files={face['files']} steps={face['steps']} pending={face['pending']} "
            f"expressible={face['expressible']} shipped_cover={face['shipped_cover']} "
            f"canonical={face['canonical']} labels={face['raw_labels']} mentions={face['mentions']}"
        )
        print(
            f"{tag} flow.branch: step={face['branch_index']} unlocks={face['branch_n']} "
            f"audited_in_it={face['branch_audited']}"
        )
    print(f"ARM-PRE flow.branch ids={pre['branch_ids']}")
    print(f"ARM-POST flow.branch ids={post['branch_ids']}")
    five = {t: post["credited"].get(t, "LOCKED (no single step)") for t in sorted(AUDITED)}
    print(f"ARM-POST credited step of the audited five={five}")
    left = sorted(set(pre["prefix"]) - set(post["prefix"]))
    entered = sorted(set(post["prefix"]) - set(pre["prefix"]))
    print(
        f"CONTROL prefix union pre={len(pre['prefix'])} post={len(post['prefix'])} "
        f"left={left} entered={entered}"
    )
    t_needs = post["locked_needs"].get(TRADIER, [])
    t_absent = [n for n in t_needs if post["statuses"].get(n) == "absent"]
    print(
        f"ARM-POST tradier: in_locked_pool={TRADIER in post['locked']} needs={t_needs} "
        f"absent_needs={t_absent} n_absent={len(t_absent)} "
        f"(pre arm in_locked_pool={TRADIER in pre['locked']})"
    )
    caps_in = sorted(set(post["step_caps"]) - set(pre["step_caps"]))
    caps_out = sorted(set(pre["step_caps"]) - set(post["step_caps"]))
    # promoted: the capabilities that newly credit one of the audited rows and were not ladder
    # members before. Adding needs cannot retire a step, so this set must equal caps_in.
    promoted = sorted(
        {post["credited"][t] for t in AUDITED if t in post["prefix"]} - set(pre["step_caps"])
    )
    print(
        f"CONTROL ladder composition steps {len(pre['step_caps'])}->{len(post['step_caps'])} "
        f"entered={caps_in} left={caps_out} promoted_by_audited={promoted}"
    )

    if set(pre["branch_ids"]) != set(AUDITED):
        failures.append(
            "the pre-repair bytes no longer credit flow.branch with exactly the audited five, "
            f"they read {pre['branch_ids']}: either the anchor revision moved or "
            "C92's ladder was not what I recorded"
        )
    if post["branch_audited"]:
        failures.append(
            f"flow.branch still unlocks audited rows after the repair: {post['branch_audited']}"
        )
    moved = [key for key in POPULATION_KEYS if pre[key] != post[key]]
    failures.extend(
        f"{key} moved between arms ({pre[key]} -> {post[key]}): the population was edited"
        for key in moved
    )
    if pre["shipped_cover"] != 0 or post["shipped_cover"] != 0:
        failures.append(
            "an arm reads a non-empty shipped-cover bucket; the over-claim face is back"
        )
    if TRADIER in post["prefix"]:
        failures.append(
            "tradier is one ladder step away although its locked need set reads "
            f"{len(t_absent)} absent kernels"
        )
    if len(t_absent) < 2:
        failures.append(
            f"tradier's locked need set reads {len(t_absent)} absent kernel(s), so a single step "
            "could unlock it and the LOCKED attribution is wrong"
        )
    if not {"shape.single_record_wrap", "derive.conditional"} <= set(t_absent):
        failures.append(
            f"the two kernels this round added to tradier are not both absent blockers: {t_absent}"
        )
    if left != [TRADIER] or entered:
        failures.append(f"the prefix movement is not the single row: left={left} entered={entered}")
    if caps_out:
        failures.append(f"capabilities left the ladder ({caps_out}) although only needs were added")
    if caps_in != promoted:
        failures.append(
            f"the ladder gained {caps_in} but the audited rows are newly credited to {promoted}"
        )

    added_total = sum(added.values())
    mention_delta = post["mentions"] - pre["mentions"]
    spelling_delta = post["raw_labels"] - pre["raw_labels"]
    # The two ladder arms deliberately share ONE registry, so the canonical count cannot differ
    # between them -- an arm that cannot differ is not a control. "The vocabulary grew by two" is
    # therefore measured against the pinned pre-repair ARTIFACT bytes, which were derived from the
    # registry as it stood before this round registered anything.
    registry_shared = pre["canonical"] == post["canonical"]
    pre_artifact = json.loads(_git(["git", "show", f"{PRE_REPAIR_REV}:{ROADMAP_REL}"]))
    artifact_canonical = pre_artifact["taxonomy"]["canonical_capability_count"]
    tree_canonical = post["canonical"]
    canonical_delta = tree_canonical - artifact_canonical
    closes = added_total == mention_delta
    two_new = canonical_delta == 2 == spelling_delta
    print(
        f"ARM-IDENTITY per-row added spellings={added} total={added_total} "
        f"judge mention delta={mention_delta} distinct-spelling delta={spelling_delta} "
        f"registry_shared_across_arms={registry_shared} (canonical={post['canonical']} both arms)"
    )
    print(
        f"ARM-IDENTITY artifact canonical at {PRE_REPAIR_REV}={artifact_canonical} "
        f"-> tree={tree_canonical} delta={canonical_delta} | artifact registry size="
        f"{len(pre_artifact['capability_registry'])} -> tree registry size={len(readings)}"
    )
    print(f"ARM-IDENTITY closes={closes} two_new_canonicals={two_new}")
    if not closes:
        failures.append(f"added spellings {added_total} != judge mention delta {mention_delta}")
    if not registry_shared:
        failures.append("the arms did not share a registry, so the ladder delta is not census-only")
    if not two_new:
        failures.append(
            "expected +2 canonicals against the pinned artifact and +2 distinct spellings "
            f"between the census arms, read {canonical_delta}/{spelling_delta}"
        )

    saved = {k: mcc.LABEL_TO_CAPABILITY[k] for k in NEW_SPELLINGS}
    refusal_orphan = ""
    try:
        for spelling in saved:
            del mcc.LABEL_TO_CAPABILITY[spelling]
        try:
            mcc.run(round_id="C93-REFUSAL")
        except (mcc.RoadmapError, ValueError) as exc:
            refusal_orphan = str(exc).splitlines()[0][:160]
        else:
            failures.append("dropping the two spellings did NOT abort: a row can lose its blocker")
    finally:
        mcc.LABEL_TO_CAPABILITY.update(saved)
    # The other side of the same boundary: with the spellings gone AND the registry entries gone,
    # the repaired rows' labels must be refused as unmapped rather than silently ignored.
    saved_registry = mcc.REGISTRY
    refusal_unmapped = ""
    try:
        for spelling in saved:
            del mcc.LABEL_TO_CAPABILITY[spelling]
        mcc.REGISTRY = tuple(e for e in saved_registry if e.capability not in NEW_CAPS)
        try:
            mcc.run(round_id="C93-REFUSAL2")
        except (mcc.RoadmapError, ValueError) as exc:
            refusal_unmapped = str(exc).splitlines()[0][:160]
        else:
            failures.append("an unmapped census label was accepted: the blocker drops quietly")
    finally:
        mcc.REGISTRY = saved_registry
        mcc.LABEL_TO_CAPABILITY.update(saved)
    after_restore = mcc.run(round_id="C93-RESTORED")
    rows_ok = after_restore.row_count == post["rows"]
    labels_ok = after_restore.labels_supported == post["labels_supported"]
    refused = bool(refusal_orphan) and bool(refusal_unmapped)
    print(f"ARM-REFUSAL spellings-only   aborted: {refusal_orphan!r}")
    print(f"ARM-REFUSAL spellings+entries aborted: {refusal_unmapped!r}")
    print(
        f"ARM-REFUSAL restored rows={after_restore.row_count}/{post['rows']} "
        f"labels_supported={after_restore.labels_supported}/{post['labels_supported']} "
        f"fully_restored={rows_ok and labels_ok}"
    )
    if not refusal_orphan:
        failures.append("the orphan-side refusal arm printed no refusal")
    if not refusal_unmapped:
        failures.append("the unmapped-label refusal arm printed no refusal")
    if not rows_ok:
        failures.append("restoring the registry did not restore the row count")
    if not labels_ok:
        failures.append(
            f"restoring the label map did not restore labels_supported: "
            f"{after_restore.labels_supported} != {post['labels_supported']}"
        )

    for reason in failures:
        print(f"FAIL {reason}")
    two_sided = all(on[c].status == "present" and forged[c].status == "absent" for c in NEW_CAPS)
    print(
        f"VERDICT pre_branch_unlocks_audited={len(pre['branch_audited'])} "
        f"post_branch_unlocks={post['branch_n']} "
        f"post_branch_unlocks_audited={len(post['branch_audited'])} "
        f"device_two_sided={two_sided} refused={refused} "
        f"tradier_absent_needs={len(t_absent)} failures={len(failures)}"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
