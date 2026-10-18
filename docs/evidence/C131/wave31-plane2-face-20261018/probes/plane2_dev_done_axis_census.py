#!/usr/bin/env python3
"""Decompose plane 2 (DEV_DONE) into the three axes that move it independently.

读的是仓库自带的判定件 ``scripts/quality/ac2_case_probe.py``，不是手算子集：

* ``measure_ac2_10``       -- AC2-10 的全部事实行，逐字进载档（本仪器不重述它的口径）
* ``engine_declared_keys`` -- 运行期声明真值，由 ``catalog.engine_declared_models()`` 从 provider
  描述符的 ``fetcher_bindings`` 派生；手维护的第二张表正是 AC2-02 拒绝的东西
* ``passing_contract_faces`` -- 真跑一遍 ``tests/test_provider_model_contracts.py``，从 junit 逐
  testcase 读正反例；skip/fail 的 testcase 不记功
* ``witnessed_dev_done``   -- declared AND positive>0 AND refusal>0

要分清的是三个各自独立、却最容易被并成一个布尔的量：运行期声明了几枚（A）、这几枚各有正反例吗
（B）、台账盖了几枚 DEV_DONE 标签（D）；plane 1 的 285 是第四个总体（C）。

臂：A 声明面直方图 + 逐枚清单；B 见证面与声明面的集合关系，``declared_without_face`` 决定
``dev_done == declared`` 是观察还是同义反复；C 跨面 join（350 身份 x 引擎声明）；D 台账对账，
D2 逐列比较盖章 5 枚与未盖章 12 枚的取值集合，``uniform_split`` 就是"台账能否自证这枚标签"；
E 终代动作图 ``implement_missing_model`` 19 行的今日声明覆盖；F 四条内存再上弦，每个读数都必须
移动，不移动的臂是装饰；G 地板：本仪器看不见什么。

不写台账、不翻标签、不设 ``OPENDATA_ALLOW_LIVE_E2E``。
"""

from __future__ import annotations

import copy
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[5]
BUNDLE = Path(__file__).resolve().parents[1]
C130_CARRIER = (
    REPO_ROOT
    / "docs/evidence/C130/plane1-action-map-rebase-20261018/runs"
    / "wave30-step5a-residue_axis_census_finalgen.txt"
)
REPORT = REPO_ROOT / "docs/evidence/C101/judge/claims.merged.report.json"
CARRIER = BUNDLE / "runs/wave31-step0-plane2-axis-census.txt"

# 根锚点先于任何臂：wave 30 因为 parents[4] 指到 docs/ 而让两条负例臂读到空值并双双"通过"。
ANCHORS = (
    "pyproject.toml",
    "scripts/quality/ac2_case_probe.py",
    "opendata/data/providers/catalog.py",
    "docs/evidence/C101/judge/claims.merged.report.json",
)

sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "quality"))

ACCEPTED = "OFFLINE_ACCEPTED"
DEV_DONE = "DEV_DONE"
#: A junit parametrisation id that names a model; the suite also emits per-face case labels.
MODEL_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*::[A-Za-z][A-Za-z0-9_]*$")
MISSING_ACTION = "implement_missing_model"
#: 终代动作图载档自己打印的本动作行数；写死是为了让"载档换代而本仪器没跟上"变成一条红灯。
EXPECTED_MISSING_ROWS = 19

JUDGE_SURFACE = (
    "measure_ac2_10",
    "judge_ac2_10",
    "engine_declared_keys",
    "passing_contract_faces",
    "witnessed_dev_done",
    "label_only_rows",
    "task_rows",
    "_ENGINE_DECLARED_RUN",
    "_CONTRACT_FACES_RUN",
)


def _premise_failures() -> list[str]:
    """Return every broken premise; a non-empty list means run no arm at all."""
    bad = [
        f"anchor_missing:{REPO_ROOT / name}" for name in ANCHORS if not (REPO_ROOT / name).is_file()
    ]
    if not C130_CARRIER.is_file():
        bad.append(f"action_map_carrier_missing:{C130_CARRIER}")
    if bad:
        return bad
    import ac2_case_probe as ac2

    return [f"judge_surface_missing:{name}" for name in JUDGE_SURFACE if not hasattr(ac2, name)]


def _key(row: dict[str, str]) -> str:
    """The join key both planes use: ``provider::upstream_model``."""
    return f"{row['provider']}::{row['upstream_model']}"


def _action_map() -> tuple[list[dict[str, str]], str]:
    """Parse the shipped action-map carrier into per-row columns, plus its own face line."""
    rows: list[dict[str, str]] = []
    face = ""
    for line in C130_CARRIER.read_text(encoding="utf-8").splitlines():
        if line.startswith("row "):
            body = line.removeprefix("row ").strip()
            fields = dict(part.split("=", 1) for part in body.split(" ") if "=" in part)
            fields["model"] = body.split(" ")[0]
            rows.append(fields)
        elif line.startswith("residue_axis_census_face "):
            face = line.strip()
    return rows, face


def _arm_a(ac2: ModuleType, out: list[str]) -> set[str]:
    declared = set(ac2.engine_declared_keys())
    hist = Counter(key.split("::")[0] for key in declared)
    out.append(
        f"A_declared_population count={len(declared)} of=350 providers={len(hist)} "
        f"histogram={dict(sorted(hist.items()))}"
    )
    out.extend(f"A  declared {key}" for key in sorted(declared))
    return declared


def _arm_b(ac2: ModuleType, declared: set[str], out: list[str]) -> tuple[set[str], dict[str, int]]:
    """Relate the executed faces to the declared set over model-shaped keys only.

    The junit id is ``test_name[case_id]``, and ``case_id`` is a parametrisation label, not always
    ``provider::Model`` -- measured here: 177 of the 202 labels are per-arm labels such as
    ``400-QUERY_REJECTED-cboe::IndexSearch``, ``face7`` or ``zip_csv``; only 25 are model-shaped.
    Counting over the unsplit 202 reports 185 labels as "faces outside the declaration" where the
    measured set is 8; the positive/refusal counters (21/21) only ever landed on model-shaped
    labels, so the split narrows the relation list and leaves the face counters unchanged.
    """
    faces = ac2.passing_contract_faces()
    shaped = {k for k in faces if MODEL_KEY_RE.match(k)}
    other = set(faces) - shaped
    positive = {k for k in shaped if faces[k][0] > 0}
    refusal = {k for k in shaped if faces[k][1] > 0}
    both = positive & refusal
    no_face = sorted(declared - shaped)
    out.append(
        f"B_face_relation suite_keys={len(faces)} model_shaped={len(shaped)} "
        f"case_label_keys={len(other)} positive={len(positive)} refusal={len(refusal)} "
        f"both={len(both)}"
    )
    out.append(
        f"B  declared={len(declared)} declared_without_face={len(no_face)} {no_face or '[]'} "
        f"model_shaped_not_declared={sorted(shaped - declared) or '[]'}"
    )
    out.append(
        f"B  model_shaped_without_both_faces={sorted(shaped - both) or '[]'} "
        f"identity model_shaped={len(shaped)} = declared {len(declared)} + "
        f"probe_rows {len(shaped - declared)}"
    )
    return declared & both, {"suite": len(faces), "shaped": len(shaped), "other": len(other)}


def _arm_c(ac2: ModuleType, declared: set[str], out: list[str]) -> dict[str, int]:
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    status = {f"{m['provider']}::{m['upstream_model']}": m["status"] for m in report["models"]}
    accepted = {k for k, v in status.items() if v == ACCEPTED}
    residue = set(status) - accepted
    off_identity = sorted(declared - set(status))
    cell = {
        "accepted_declared": len(accepted & declared),
        "accepted_not_declared": len(accepted - declared),
        "residue_declared": len(residue & declared),
        "residue_not_declared": len(residue - declared),
    }
    out.append(
        f"C_cross_plane_join identities={len(status)} accepted={len(accepted)} "
        f"residue={len(residue)} declared={len(declared)} 2x2={cell} "
        f"declared_outside_identity_set={len(off_identity)} {off_identity or ''} "
        f"status_counts={report['summary']['status_counts']} "
        f"identity_set={report['inventory']['identity_set_sha256'][:16]}"
    )
    totals: dict[str, dict[str, int]] = {}
    for key, value in status.items():
        entry = totals.setdefault(key.split("::")[0], {"rows": 0, "accepted": 0, "declared": 0})
        entry["rows"] += 1
        entry["accepted"] += 1 if value == ACCEPTED else 0
        entry["declared"] += 1 if key in declared else 0
    for provider in sorted(totals):
        entry = totals[provider]
        out.append(
            f"C  provider {provider} rows={entry['rows']} accepted={entry['accepted']} "
            f"declared={entry['declared']}"
        )
    return cell


def _label_split(
    rows: list[dict[str, str]], declared: set[str], labelled_keys: set[str]
) -> tuple[int, list[tuple[str, set[str], set[str]]], list[str]]:
    """Compare each ledger column across stamped vs unstamped declared rows.

    Returns ``(columns_compared, columns_whose_value_sets_differ, columns_that_split_uniformly)``.
    The third list is the only one that could explain the stamp from the book itself: a column whose
    labelled rows hold exactly one value and whose unlabelled rows hold exactly one other value.
    """
    lab = [r for r in rows if _key(r) in declared and _key(r) in labelled_keys]
    unl = [r for r in rows if _key(r) in declared and _key(r) not in labelled_keys]
    pairs = [(c, {r.get(c, "") for r in lab}, {r.get(c, "") for r in unl}) for c in rows[0]]
    differing = [t for t in pairs if t[0] != "implementation_task_status" and t[1] != t[2]]
    uniform = [t[0] for t in differing if len(t[1]) == 1 and len(t[2]) == 1]
    return len(pairs) - 1, differing, uniform


def _arm_d(
    ac2: ModuleType, rows: list[dict[str, str]], declared: set[str], out: list[str]
) -> dict[str, int]:
    witnessed = ac2.witnessed_dev_done(rows)
    census = Counter(r["implementation_task_status"] for r in rows)
    naked = ac2.label_only_rows(rows, witnessed)
    labelled = [r for r in rows if r["implementation_task_status"] == DEV_DONE]
    labelled_keys = {_key(r) for r in labelled}
    unlabelled = sorted(declared - labelled_keys)
    labelled_not_declared = sorted(labelled_keys - declared)
    out.append(
        f"D_ledger_reconcile rows={len(rows)} status_histogram={dict(sorted(census.items()))} "
        f"witnessed={len(witnessed)} label_without_witness={len(naked)} "
        f"declared={len(declared)} declared_and_labelled={len(declared & labelled_keys)} "
        f"declared_not_labelled={len(unlabelled)}"
    )
    for model in unlabelled:
        entry = next(r for r in rows if _key(r) == model)
        out.append(
            f"D  declared_but_unlabelled {model} status={entry['implementation_task_status']} "
            f"task={entry['task_id']}"
        )
    out.append(
        f"D  labelled_not_declared={labelled_not_declared or '[]'} "
        f"witnessed_task_ids_not_labelled="
        f"{len(sorted(witnessed - {r['task_id'] for r in labelled}))}"
    )
    compared, differing, uniform = _label_split(rows, declared, labelled_keys)
    out.append(
        f"D2_label_exposition labelled={len(declared & labelled_keys)} "
        f"unlabelled={len(unlabelled)} columns_compared={compared} "
        f"value_set_differing={len(differing)} uniform_split={len(uniform)} {uniform or '[]'}"
    )
    out.extend(
        f"D2  col={col} labelled_only={sorted(a - b) or '[]'} "
        f"unlabelled_only={sorted(b - a) or '[]'}"
        for col, a, b in differing
    )
    return {
        "rows": len(rows),
        "witnessed": len(witnessed),
        "naked": len(naked),
        "declared": len(declared),
        "declared_and_labelled": len(declared & labelled_keys),
        "declared_not_labelled": len(unlabelled),
        "labelled": len(labelled_keys),
        "value_set_differing": len(differing),
        "uniform_split": len(uniform),
    }


def _arm_e(declared: set[str], out: list[str]) -> tuple[int, int]:
    rows, face = _action_map()
    missing = [r for r in rows if r["action"] == MISSING_ACTION]
    hist = Counter(k.split("::")[0] for k in declared)
    out.append(
        f"E_action_map parsed_rows={len(rows)} implement_missing_model={len(missing)} "
        f"expected={EXPECTED_MISSING_ROWS} carrier_face={face[:120]}"
    )
    for row in sorted(missing, key=lambda r: r["model"]):
        provider = row["model"].split("::")[0]
        out.append(
            f"E  row {row['model']} status={row['status']} verdict={row['verdict']} "
            f"seam={row['seam']} module={row['witness_module']} "
            f"provider_declared_today={hist.get(provider, 0)}"
        )
    by_provider = Counter(r["model"].split("::")[0] for r in missing)
    covered = sum(count for provider, count in by_provider.items() if hist.get(provider))
    out.append(
        f"E  by_provider={dict(sorted(by_provider.items()))} "
        f"rows_in_a_provider_that_already_declares_something={covered} of={len(missing)}"
    )
    return len(missing), covered


def _arm_f(
    ac2: ModuleType, rows: list[dict[str, str]], declared: set[str], out: list[str]
) -> list[bool]:
    """In-memory re-arm: each mutation moves exactly one reading, then is restored."""
    declared_cache = ac2._ENGINE_DECLARED_RUN[0]
    faces_cache = ac2._CONTRACT_FACES_RUN[0]
    baseline = len(ac2.witnessed_dev_done(rows))
    naked_rows = ac2.label_only_rows(rows, ac2.witnessed_dev_done(rows))
    naked_baseline = len(naked_rows)
    victim = sorted(declared)[0]
    verdicts: list[bool] = []

    try:
        ac2._ENGINE_DECLARED_RUN[0] = {k for k in declared_cache if k != victim}
        moved = len(ac2.witnessed_dev_done(rows))
        ok = moved == baseline - 1
        verdicts.append(ok)
        out.append(
            f"F1_drop_declaration victim={victim} witnessed {baseline} -> {moved} "
            f"expected={baseline - 1} -> {ok}"
        )
    finally:
        ac2._ENGINE_DECLARED_RUN[0] = declared_cache

    try:
        positive, _refusal = faces_cache.get(victim, (0, 0))
        ac2._CONTRACT_FACES_RUN[0] = {**faces_cache, victim: (positive, 0)}
        moved = len(ac2.witnessed_dev_done(rows))
        ok = moved == baseline - 1
        verdicts.append(ok)
        out.append(
            f"F2_zero_refusal_face victim={victim} witnessed {baseline} -> {moved} "
            f"expected={baseline - 1} -> {ok}"
        )
    finally:
        ac2._CONTRACT_FACES_RUN[0] = faces_cache

    forged = copy.deepcopy(rows)
    witnessed_now = ac2.witnessed_dev_done(forged)
    victim_row = next((r for r in forged if r["task_id"] not in witnessed_now), None)
    if victim_row is None:
        verdicts.append(False)
        out.append(
            f"F3_forge_dev_done_label SKIPPED denominator={len(forged)} "
            "-> False (every ledger row already has a witness, so the label axis cannot be armed)"
        )
    else:
        victim_row["implementation_task_status"] = DEV_DONE
        moved = len(ac2.label_only_rows(forged, ac2.witnessed_dev_done(forged)))
        ok = moved == naked_baseline + 1
        verdicts.append(ok)
        out.append(
            f"F3_forge_dev_done_label victim={_key(victim_row)} task={victim_row['task_id']} "
            f"label_without_witness {naked_baseline} -> {moved} "
            f"expected={naked_baseline + 1} -> {ok}"
        )

    lab_keys = {
        _key(r) for r in rows if _key(r) in declared and r["implementation_task_status"] == DEV_DONE
    }
    _comp, b_diff, b_uniform = _label_split(rows, declared, lab_keys)
    tampered = copy.deepcopy(rows)
    for row in tampered:
        if _key(row) in declared and _key(row) not in lab_keys:
            row["credential_fields"] = "api_key"
    t_compared, t_diff, t_uniform = _label_split(tampered, declared, lab_keys)
    ok = t_uniform == ["credential_fields"]
    verdicts.append(ok)
    out.append(
        f"F5_forge_explanatory_column labelled={len(lab_keys)} "
        f"differing {len(b_diff)} -> {len(t_diff)} uniform {len(b_uniform)} -> {len(t_uniform)} "
        f"{t_uniform} columns_compared={t_compared} expected=['credential_fields'] -> {ok}"
    )

    restored = (
        ac2._ENGINE_DECLARED_RUN[0] is declared_cache and ac2._CONTRACT_FACES_RUN[0] is faces_cache
    )
    after = len(ac2.witnessed_dev_done(rows))
    verdicts.append(restored and after == baseline)
    out.append(
        f"F4_restore caches_identical={restored} witnessed_after={after} baseline={baseline} "
        f"-> {restored and after == baseline}"
    )
    return verdicts


def _arm_g(ac2: ModuleType, out: list[str]) -> None:
    facts = ac2.measure_ac2_10()
    files = facts["contract_test_files"].split(",")
    out.append(
        f"G_floor contract_test_files={len(files)} "
        f"functions_in_them={facts['contract_test_functions']} "
        f"suite_executed_for_faces=1(tests/test_provider_model_contracts.py) "
        f"live_verification_run={facts['live_verification_run']} "
        f"engine_declared_rows_in_ledger={facts['engine_declared_rows']}"
    )
    out.append(
        "G  floor: AC2-10 counts a witness only through engine_declared_keys, so a hand-written "
        "fetcher passing its own provider suite reads NOT_RUN here; and a live upstream leg needs "
        "the exact OPENDATA_ALLOW_LIVE_E2E token, which this wave does not set."
    )


def main() -> int:
    """Check the premises first, then run the arms, and always leave a carrier behind."""
    lines = [
        "wave-31 step 0 -- what does plane 2 read, and along which axis?",
        f"argv: PYTHONPATH= {sys.executable} docs/evidence/C131/"
        f"wave31-plane2-face-20261018/probes/{Path(__file__).name}",
        f"repo_root={REPO_ROOT} judge=scripts/quality/ac2_case_probe.py "
        f"report={REPORT.relative_to(REPO_ROOT)} "
        f"carrier_of_action_map={C130_CARRIER.relative_to(REPO_ROOT)}",
    ]
    broken = _premise_failures()
    if broken:
        lines.append(f"premise_broken=1 reasons={broken} arms_run=0")
        lines.append("plane2_axis_census_face arms=0 arms_passed=0 arms_failed=0 PASS=False")
        lines.append("plane2_axis_census_exit=1")
        _emit(lines)
        return 1

    import ac2_case_probe as ac2

    try:
        return _measure(ac2, lines)
    except Exception as exc:
        lines.append(f"crash=1 error={type(exc).__name__}: {exc}")
        lines.append("plane2_axis_census_face arms=-1 arms_passed=-1 arms_failed=-1 PASS=False")
        lines.append("plane2_axis_census_exit=1")
        _emit(lines)
        return 1


def _measure(ac2: ModuleType, lines: list[str]) -> int:
    """Run every arm, gate on real predicates, write the carrier, and return the exit code."""
    facts = ac2.measure_ac2_10()
    verdict = ac2.judge_ac2_10(facts)
    lines.append("0_ac2_10_verbatim (the repo's own reading, unedited):")
    lines.extend(f"0  {reading}" for reading in verdict.readings)
    lines.append(f"0  state={verdict.state} reason={verdict.reason}")

    declared = _arm_a(ac2, lines)
    witnessed_set, bsplit = _arm_b(ac2, declared, lines)
    rows = ac2.task_rows()
    cell = _arm_c(ac2, declared, lines)
    book = _arm_d(ac2, rows, declared, lines)
    missing_rows, covered = _arm_e(declared, lines)
    verdicts = _arm_f(ac2, rows, declared, lines)
    _arm_g(ac2, lines)

    checks = [
        ("every_declaration_has_both_faces", len(declared - witnessed_set) == 0),
        ("dev_done_is_the_declared_both_face_set", book["witnessed"] == len(witnessed_set)),
        ("ledger_rows_are_350", book["rows"] == 350),
        ("no_dev_done_label_without_witness", book["naked"] == 0),
        (
            "declaration_splits_labelled_and_unlabelled",
            book["declared"] == book["declared_and_labelled"] + book["declared_not_labelled"],
        ),
        (
            "accepted_plus_residue_is_the_identity_set",
            sum(cell.values()) == 350,
        ),
        (
            "every_declaration_is_one_of_the_350",
            len(declared) == cell["accepted_declared"] + cell["residue_declared"],
        ),
        (
            "action_map_carrier_still_the_wave30_final_generation",
            missing_rows == EXPECTED_MISSING_ROWS,
        ),
        (
            "junit_key_split_is_exact",
            bsplit["shaped"] + bsplit["other"] == bsplit["suite"],
        ),
        (
            "no_ledger_column_explains_the_dev_done_label",
            book["uniform_split"] == 0,
        ),
    ]
    for name, ok in checks:
        lines.append(f"X_gate {name} -> {ok}")
    total = len(verdicts) + len(checks)
    passed = sum(1 for ok in verdicts if ok) + sum(1 for _n, ok in checks if ok)
    lines.append(
        f"plane2_axis_census_face arms={total} arms_passed={passed} arms_failed={total - passed} "
        f"declared={len(declared)} dev_done={book['witnessed']} dev_done_label={book['labelled']} "
        f"label_without_witness={book['naked']} accepted_declared={cell['accepted_declared']} "
        f"label_columns_differing={book['value_set_differing']} "
        f"label_uniform_split={book['uniform_split']} "
        f"missing_model_rows={missing_rows} rows_in_declaring_provider={covered} "
        f"PASS={passed == total}"
    )
    lines.append("plane2_axis_census_exit=0" if passed == total else "plane2_axis_census_exit=1")
    _emit(lines)
    return 0 if passed == total else 1


def _emit(lines: list[str]) -> None:
    """Write the carrier once, then echo it with the tool's own line count."""
    CARRIER.parent.mkdir(parents=True, exist_ok=True)
    CARRIER.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for line in lines:
        print(line)
    print(f"carrier_written bytes={CARRIER.stat().st_size} lines={len(lines)}")


if __name__ == "__main__":
    sys.exit(main())
