"""C94 judgment face: which rows ``flow.fan_out`` is actually credited with unlocking.

Five rows were credited to ``flow.fan_out`` in C93 (step 0, newly=5), which read as "build this
kernel and ship five rows". This round audited those five line by line against the pinned upstream
models and repaired four need sets; the face this instrument publishes is the *movement* of that
credit, measured twice with the repo's own judge -- once over ``PRE_REV``'s census bytes and once
over the working tree -- so the number comes from the judge rather than from my arithmetic.

Both runs use the SAME judge and vocabulary, so the only difference between the two readings is the
census bytes. That is what makes the delta attributable: the five spellings added this round fold
onto capabilities that already existed, so none of them can change a capability's status.

Arms, and the target that makes each one able to differ:
  ARM-CREDIT     pre = the 5 audited rows; post = exactly 1 of them. Same predicate, other tree.
  ARM-FORGE-UP   revert the sec row to its pinned pre-repair bytes inside a copy of the repaired
                 tree -> it must re-enter the credit list. Proves the post reading responds to the
                 labels rather than to a constant I typed.
  ARM-FORGE-DOWN give the one still-credited audited row a single absent need -> it must LEAVE the
                 list. The other side: an arm can only witness a number it is able to move.
  ARM-REFUSAL    an unmapped spelling must abort the run -> proves the guard bites, so the 5 new
                 folds being mapped is what makes the green green.
  ARM-ARITH      the judge's own mention delta must equal the sum of the per-row need deltas,
                 computed independently from the two trees.
  ARM-VOCAB      no spelling present pre may be absent post (nothing was deleted).
  FLOOR          printed, not asserted: the non-audited rows sharing the credit, and the fact that
                 this device reads label folds, not engine behaviour.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import shutil
import subprocess  # nosec B404  # read-only git argv, shell disabled
import sys
import tempfile
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "quality"))

import model_capability_census as mcc  # noqa: E402

_C93_PATH = REPO_ROOT / "docs" / "evidence" / "C93" / "flow-branch-need-face.py"
_spec = importlib.util.spec_from_file_location("c93_face", _C93_PATH)
if _spec is None or _spec.loader is None:  # pragma: no cover
    raise SystemExit(f"cannot load the committed C93 instrument from {_C93_PATH}")
c93 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(c93)

PRE_REV = "e2ed7b2"  # pinned: the tree BEFORE this round's need-set repair (never HEAD~1)
CAPABILITY = "flow.fan_out"
AUDITED = {
    "OBB2-deribit-FuturesInfo",
    "OBB2-intrinio-EquityInfo",
    "OBB2-intrinio-EquityQuote",
    "OBB2-sec-LatestFinancialReports",
    "OBB2-tiingo-CurrencyHistorical",
}
FAN_OUT_SPELLINGS = {k for k, v in mcc.LABEL_TO_CAPABILITY.items() if v == "flow.fan_out"}


def _face(census_dir: pathlib.Path | None) -> dict[str, Any]:
    """Run the repo's judge over one census directory and return the faces this archive reads."""
    report = mcc.run(round_id="C94-CONTROL", census_dir=census_dir)
    return dict[str, Any](json.loads(mcc.serialize(report)))


def _credit(face: dict[str, Any], capability: str) -> tuple[int, list[str]]:
    steps = face["roadmap"]["greedy_build_order"]
    index = next((i for i, s in enumerate(steps) if s["capability"] == capability), -1)
    if index < 0:
        return -1, []
    step = steps[index]
    return index, sorted(r["task_id"] for r in step.get("newly_unlocked_rows", []))


def _needs_by_row(census_dir: pathlib.Path | None) -> dict[str, list[str]]:
    """Read each row's raw need list straight off one census tree, independent of the judge."""
    root = census_dir if census_dir is not None else REPO_ROOT / c93.CENSUS_REL
    out: dict[str, list[str]] = {}
    for path in sorted(root.glob("census-*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        for row in payload.get("rows", []):
            if isinstance(row, dict) and row.get("task_id"):
                out[row["task_id"]] = list(row["needs_engine_capability"])
    return out


def _spellings(census_dir: pathlib.Path | None) -> set[str]:
    return {label for labels in _needs_by_row(census_dir).values() for label in labels}


def _forge_sec_back(
    pre_dir: pathlib.Path, post_snapshot: pathlib.Path, outdir: pathlib.Path
) -> list[str]:
    """Restore the sec row's WHOLE pre-repair need list inside a copy of the repaired tree.

    Appending only the refuted ``multi_request_fanout`` is not an arm: the repair legitimately gave
    that row three further absent needs, so one restored label cannot re-credit it and the arm
    reads False for a reason that has nothing to do with the device. Replacing the row's list with
    the pinned pre-repair list isolates the cause -- same predicate, same object, only the bytes
    of that one row differ.
    """
    outdir.mkdir(parents=True, exist_ok=True)
    for path in sorted(post_snapshot.glob("census-*.json")):
        shutil.copy2(path, outdir / path.name)
    target = outdir / "census-sec-tmx-fed-gov-finra.json"
    payload = json.loads(target.read_text(encoding="utf-8"))
    pre_path = pre_dir / "census-sec-tmx-fed-gov-finra.json"
    pre_payload = json.loads(pre_path.read_text(encoding="utf-8"))
    before = next(
        r["needs_engine_capability"]
        for r in pre_payload["rows"]
        if r.get("task_id") == "OBB2-sec-LatestFinancialReports"
    )
    for row in payload["rows"]:
        if row.get("task_id") == "OBB2-sec-LatestFinancialReports":
            row["needs_engine_capability"] = list(before)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return list(before)


def _suppress_equityinfo(post_snapshot: pathlib.Path, outdir: pathlib.Path) -> str:
    """Give the one credited audited row an absent need that lies OUTSIDE the step-4 prefix.

    The first attempt used ``sentinel_value_nulling`` and the row stayed credited: the greedy
    ladder puts ``columns.null_sentinel`` at step 0, so by step 4 that need is already inside the
    engine pool the step is measured against. A need can only un-credit a row when it is not in
    the prefix, which is what ``columns.compose`` (step 25) is. Same reason the arm is written as
    membership rather than length: ``newly`` is a cover position, not a count of rows needing the
    capability, and reverting one row reshuffles which step another row is attributed to.
    """
    outdir.mkdir(parents=True, exist_ok=True)
    for path in sorted(post_snapshot.glob("census-*.json")):
        shutil.copy2(path, outdir / path.name)
    target = outdir / "census-intrinio-1of3.json"
    payload = json.loads(target.read_text(encoding="utf-8"))
    for row in payload["rows"]:
        if row.get("task_id") == "OBB2-intrinio-EquityInfo":
            row["needs_engine_capability"].append("code_table_reverse_lookup")
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return "code_table_reverse_lookup"


def _plant_unmapped(pre_dir: pathlib.Path, outdir: pathlib.Path) -> None:
    """Copy a census tree and give one row a spelling the vocabulary does not know."""
    shutil.copytree(pre_dir, outdir)
    name = "census-tiingo.json"
    path = outdir / name
    payload = json.loads(path.read_text(encoding="utf-8"))
    for row in payload["rows"]:
        if row.get("task_id") == "OBB2-tiingo-CurrencyHistorical":
            row["needs_engine_capability"].append("c94_unmapped_probe_label")
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _heterogeneity(census_dir: pathlib.Path | None) -> dict[str, int]:
    """Split the fan_out reference surface by spelling: the denominator is not one mechanism."""
    counts = dict.fromkeys(sorted(FAN_OUT_SPELLINGS), 0)
    rows = paged = joined = 0
    for path in sorted(
        (census_dir if census_dir is not None else REPO_ROOT / c93.CENSUS_REL).glob("census-*.json")
    ):
        payload = json.loads(path.read_text(encoding="utf-8"))
        for row in payload.get("rows", []):
            labels = set(row.get("needs_engine_capability", []))
            if not labels & FAN_OUT_SPELLINGS:
                continue
            rows += 1
            for spelling in labels & FAN_OUT_SPELLINGS:
                counts[spelling] += 1
            if row.get("paged") is True:
                paged += 1
            if row.get("join_needed") is True:
                joined += 1
    return {"rows": rows, "paged_true": paged, "join_needed_true": joined, **counts}


def main() -> int:
    """Publish the credit face for both trees and every arm's own verdict line."""
    base = pathlib.Path(tempfile.mkdtemp(prefix="c94_face_"))
    pre_dir = base / "pre"
    n_files = c93._materialize_pre_repair(PRE_REV, pre_dir)
    ver = subprocess.run(  # nosec B603, B607  # noqa: S603
        ["git", "rev-parse", "--short", PRE_REV],  # noqa: S607
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    print(f"PRE_REV {PRE_REV} resolves_to={ver} census_files={n_files}")

    pre_face = _face(pre_dir)
    post_face = _face(None)
    pre_index, pre_credit = _credit(pre_face, CAPABILITY)
    post_index, post_credit = _credit(post_face, CAPABILITY)

    print(f"\nARM-CREDIT pre  {CAPABILITY} step={pre_index} newly={len(pre_credit)} {pre_credit}")
    print(f"ARM-CREDIT post {CAPABILITY} step={post_index} newly={len(post_credit)} {post_credit}")
    pre_audited = sorted(set(pre_credit) & AUDITED)
    post_audited = sorted(set(post_credit) & AUDITED)
    print(f"ARM-CREDIT audited rows credited pre={len(pre_audited)} {pre_audited}")
    print(f"ARM-CREDIT audited rows credited post={len(post_audited)} {post_audited}")
    credit_ok = pre_audited == sorted(AUDITED) and post_audited == ["OBB2-intrinio-EquityInfo"]

    forge_dir = base / "forge"
    post_snapshot = base / "post"
    post_snapshot.mkdir()
    for path in sorted((REPO_ROOT / c93.CENSUS_REL).glob("census-*.json")):
        shutil.copy2(path, post_snapshot / path.name)
    restored = _forge_sec_back(pre_dir, post_snapshot, forge_dir)
    _, forge_credit = _credit(_face(forge_dir), CAPABILITY)
    forge_audited = sorted(set(forge_credit) & AUDITED)
    print(
        f"ARM-FORGE-UP sec row reverted to pre bytes {restored} -> newly={len(forge_credit)} "
        f"audited={forge_audited} "
        f"sec_back_in_credit={'OBB2-sec-LatestFinancialReports' in forge_credit}"
    )
    forge_ok = "OBB2-sec-LatestFinancialReports" in forge_credit
    print(
        f"ARM-FORGE-UP set vs post: gained={sorted(set(forge_credit) - set(post_credit))} "
        f"lost={sorted(set(post_credit) - set(forge_credit))} "
        f"(newly is a cover position, not a count: {len(post_credit)} -> {len(forge_credit)})"
    )

    suppress_dir = base / "suppress"
    planted = _suppress_equityinfo(post_snapshot, suppress_dir)
    suppress_index, suppress_credit = _credit(_face(suppress_dir), CAPABILITY)
    print(
        f"ARM-FORGE-DOWN EquityInfo += {planted} (columns.compose, outside the step-{post_index} "
        f"prefix) -> newly={len(suppress_credit)} step={suppress_index} "
        f"equityinfo_left_credit={'OBB2-intrinio-EquityInfo' not in suppress_credit}"
    )
    suppress_ok = "OBB2-intrinio-EquityInfo" not in suppress_credit

    unmapped_dir = base / "unmapped"
    _plant_unmapped(post_snapshot, unmapped_dir)
    refusal = "no-abort"
    try:
        _face(unmapped_dir)
    except Exception as exc:
        refusal = f"{type(exc).__name__}: {str(exc)[:120]}"
    print(f"ARM-REFUSAL unmapped spelling -> {refusal}")
    refusal_ok = refusal != "no-abort"

    pre_needs, post_needs = _needs_by_row(pre_dir), _needs_by_row(None)
    row_delta = sum(
        len(post_needs.get(task_id, [])) - len(pre_needs.get(task_id, [])) for task_id in post_needs
    )
    mention_delta = (
        post_face["taxonomy"]["raw_label_mention_count"]
        - pre_face["taxonomy"]["raw_label_mention_count"]
    )
    print(
        f"ARM-ARITH judge mentions {pre_face['taxonomy']['raw_label_mention_count']}->"
        f"{post_face['taxonomy']['raw_label_mention_count']} delta={mention_delta} | "
        f"per-row need delta sum={row_delta} | equal={mention_delta == row_delta}"
    )
    print(
        f"ARM-ARITH canonical {pre_face['taxonomy']['canonical_capability_count']}->"
        f"{post_face['taxonomy']['canonical_capability_count']} | raw spellings "
        f"{pre_face['taxonomy']['raw_label_count']}->{post_face['taxonomy']['raw_label_count']} | "
        f"rows {pre_face['census']['row_count_measured']}->"
        f"{post_face['census']['row_count_measured']}"
    )
    arith_ok = (
        mention_delta == row_delta
        and post_face["taxonomy"]["canonical_capability_count"]
        == pre_face["taxonomy"]["canonical_capability_count"]
        and post_face["census"]["row_count_measured"] == pre_face["census"]["row_count_measured"]
    )

    pre_spellings, post_spellings = _spellings(pre_dir), _spellings(None)
    removed = sorted(pre_spellings - post_spellings)
    added = sorted(post_spellings - pre_spellings)
    print(f"ARM-VOCAB added={added} removed={removed} (removed must be empty)")
    vocab_ok = not removed

    print("\nARM-HETEROGENEITY fan_out reference surface (working tree):")
    for key, value in _heterogeneity(None).items():
        print(f"    {key}={value}")
    print(f"ARM-HETEROGENEITY pre surface rows={_heterogeneity(pre_dir)['rows']}")

    statuses = {k: v["status"] for k, v in post_face["capability_registry"].items()}
    present = sorted(k for k, s in statuses.items() if s == "present")
    print(f"FACE present capabilities n={len(present)}: {present}")
    print(
        f"FACE rows_expressible_today={post_face['roadmap']['rows_expressible_today']} "
        f"pending={post_face['roadmap']['rows_pending']} "
        f"prefix={post_face['roadmap']['rows_unlocked_by_greedy_prefix']} "
        f"still_locked={len(post_face['roadmap']['rows_still_locked_after_engine_pool'])}"
    )

    # Derived, falsifiable prediction for the next round: a fan_out kernel built TODAY, with no
    # other absent capability shipped, flips exactly the rows whose whole need set is
    # present + fan_out. That set is computed from the tree, not typed.
    covered = {k for k, s in statuses.items() if s == "present"}
    flips = sorted(
        task_id
        for task_id, labels in post_needs.items()
        if labels and {mcc.LABEL_TO_CAPABILITY[s] for s in labels} - covered == {CAPABILITY}
    )
    print(f"PREDICT building {CAPABILITY} alone flips n={len(flips)} {flips}")
    print(
        f"PREDICT void condition: any of these rows gaining one absent need, or a step-0..3 "
        f"capability shipping first (then the credit list is a different set: "
        f"post newly={len(post_credit)})"
    )

    print("\nFLOOR this device cannot see:")
    non_audited = sorted(set(post_credit) - AUDITED)
    print(f"    rows sharing the credit that this round did NOT audit: {non_audited}")
    print("    it reads label folds and set cover, never engine behaviour; no request was made")

    all_ok = credit_ok and forge_ok and suppress_ok and refusal_ok and arith_ok and vocab_ok
    print(
        f"VERDICT credit_moved={credit_ok} forge_up={forge_ok} forge_down={suppress_ok} "
        f"refusal_bites={refusal_ok} arith_identity={arith_ok} vocab_only_additive={vocab_ok} "
        f"all={all_ok}"
    )
    shutil.rmtree(base, ignore_errors=True)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
