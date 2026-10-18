"""Prove that wave-31 step 5 consumed the negative control by ACCEPTING it, and that arm B now sees.

``docs/evidence/C128/plane1-residue-readiness-20261017/probes/incomplete_row_guard_check.py`` gated
``B_landed_negative`` on one reading: ``CONTROL_NEGATIVE not in TRIPPABLE``. It named
``tmx::EquitySearch``, the row this wave landed. Wave-31 step 3's re-arm could cite a verdict move
(``NO_CODE_SITE`` -> ``DOCUMENT_TRIPPED``) as the reason its control died; this wave has no such
move to cite, and measuring that is the point of this probe. ``EquitySearchFetcher`` was accepted by
declaring ``row_model`` and giving the row a witness entry -- the list-shape guard this wave first
drafted was withdrawn once it measured as dead code (``client.list_rows`` refuses a non-list payload
with ``TMX_BAD_ROWS`` before the mapper runs), so the shipped classifier still reads the landed row
as ``NO_CODE_SITE``, which the old arm B predicate accepts. The control died by leaving the residue,
and an arm that only reads verdicts cannot see that: it would certify a row the census never visits.

Four arms, all of which can fail:

``A_consumed_control``
    the OLD label reads ``NO_CODE_SITE`` (verdict unchanged by the landing), ``OFFLINE_ACCEPTED``
    (so it is not residue), and the new membership clause refuses it while the verdict-only clause
    still passes it. The last pair is the falsification: the hardening is priced by the case it
    rejects, not by the case it keeps.
``B_rearmed_control``
    the NEW ``CONTROL_NEGATIVE`` reads non-trippable, is a member of the residue, and keeps arm C
    alive: class-scoped ``transform_data`` count 0 against a provider-package screen that is not.
``C_no_dead_code``
    the shipped mapper carries no inline raise in ``EquitySearchFetcher.transform_data`` (counted on
    the AST) and the withdrawn guard's own condition still refuses from the client layer: a non-list
    payload raises ``TMX_BAD_ROWS`` with a frame outside the fetcher. Both halves are needed --
    "the guard is gone" without "the refusal did not go with it" would describe an unguarded row.
``D_tamper_bites``
    the entry's declared tamper is applied by the shipped runner's own ``_apply_tamper`` and refuses
    ``TMX_BAD_ROW``, while the two-sided control -- the same cell set to null instead of removed --
    serves two rows. That contrast is what ``tamper_reason`` claims, so the published prose is
    checked against a reading that would refute it.

Everything measured here comes from the shipped classifier, the shipped runner and the shipped
fetcher; this probe defines no verdicts of its own.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

REPO_ROOT = Path(__file__).resolve().parents[5]
INSTRUMENT_DIR = REPO_ROOT / "docs/evidence/C128/plane1-residue-readiness-20261017/probes"
INSTRUMENT = INSTRUMENT_DIR / "incomplete_row_guard_check.py"
SCRIPTS = REPO_ROOT / "scripts/quality"
FETCHERS = REPO_ROOT / "opendata/data/providers/tmx/models/fetchers.py"
OLD_CONTROL = "tmx::EquitySearch"
CLASS_NAME = "EquitySearchFetcher"

sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(INSTRUMENT_DIR))
sys.path.insert(0, str(SCRIPTS))

import incomplete_row_guard_check as probe  # noqa: E402  # the shipped C128 classifier
import iter2_claims_probe_runner as runner  # noqa: E402  # the shipped phase runner
import iter2_model_evidence as judge  # noqa: E402  # the repo's judge: it owns the identity pin

from opendata.data.providers.tmx.models import client as tmx_client  # noqa: E402
from opendata.data.providers.tmx.models.client import GraphqlReply  # noqa: E402
from opendata.data.providers.tmx.models.fetchers import (  # noqa: E402
    EquitySearchFetcher,
    EquitySearchQuery,
)

ROOT_FIELD = "findTmxCompaniesByKeywords"


def inline_raises(path: Path, class_name: str, method: str) -> list[int]:
    """Line numbers of ``raise`` statements written inside one method of one class."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if not (isinstance(node, ast.ClassDef) and node.name == class_name):
            continue
        for member in node.body:
            if isinstance(member, ast.FunctionDef) and member.name == method:
                return [
                    r.lineno
                    for r in ast.walk(member)
                    if isinstance(r, ast.Raise) and member.lineno <= r.lineno <= member.end_lineno
                ]
    raise AssertionError(f"{class_name}.{method} not found in {path}")


def refusal(fn: Callable[[], object]) -> tuple[str, str, str]:
    """Call a zero-argument callable, returning (kind, code, raising-module) or a SERVED face."""
    try:
        result = fn()
    except BaseException as exc:  # the refusal face IS the measurement
        frame = exc.__traceback__
        deepest = frame
        while deepest is not None and deepest.tb_next is not None:
            deepest = deepest.tb_next
        module = "" if deepest is None else deepest.tb_frame.f_globals.get("__name__", "")
        return type(exc).__name__, str(getattr(exc, "code", "")), module
    return "SERVED", f"rows={len(result)}", ""


def main() -> int:
    """Print the four faces and gate on the pattern the landing actually produced."""
    print(
        "argv: "
        + sys.executable
        + " "
        + str(Path(__file__).resolve())
        + "   (cwd = repo root, no pipes, PYTHONPATH=.)"
    )
    print(f"instrument={INSTRUMENT.relative_to(REPO_ROOT)} exists={INSTRUMENT.is_file()}")

    report = probe.seam._load_report()
    judged: dict[str, str] = {str(r["model_identity"]): str(r["status"]) for r in report["models"]}
    residue = {identity for identity, status in judged.items() if status != "OFFLINE_ACCEPTED"}
    identity_set = report["inventory"]["expected_identity_set_sha256"][:16]
    status_counts = report["summary"]["status_counts"]
    print(
        f"POPULATION report_rows={len(report['models'])} judged={len(judged)} "
        f"summary={dict(report['summary'])} status_counts={dict(status_counts)} "
        f"identity_set={identity_set} old_in_residue={OLD_CONTROL in residue} "
        f"new_in_residue={probe.CONTROL_NEGATIVE in residue}"
    )

    old_row = probe._row(OLD_CONTROL, judged.get(OLD_CONTROL, "?"))
    old_verdict_ok = str(old_row["verdict"]) not in probe.TRIPPABLE
    old_membership_ok = OLD_CONTROL in residue
    arm_a = (
        str(old_row["verdict"]) == "NO_CODE_SITE"
        and judged.get(OLD_CONTROL) == "OFFLINE_ACCEPTED"
        and old_verdict_ok
        and not old_membership_ok
    )
    print(
        f"A_consumed_control {OLD_CONTROL} verdict={old_row['verdict']} "
        f"judge_status={judged.get(OLD_CONTROL)} class_counts={old_row.get('counts')} "
        f"code_raises={old_row.get('code_raises')} raises={old_row.get('raises')} "
        f"verdict_only_arm_b={old_verdict_ok} membership_arm_b={old_membership_ok} "
        f"new_clause_refuses_old_label={old_verdict_ok and not old_membership_ok} PASS={arm_a}"
    )

    new_label = str(probe.CONTROL_NEGATIVE)
    new_row = probe._row(new_label, judged.get(new_label, "?"))
    class_td = int((new_row.get("counts") or {}).get("transform_data", 0))
    new_screen = probe._provider_screen(new_label.partition("::")[0])
    package_td = int(new_screen.get("transform_data", 0))
    arm_b = (
        str(new_row["verdict"]) not in probe.TRIPPABLE
        and new_label in residue
        and class_td == 0
        and package_td > 0
        and bool(new_row["bound"])
    )
    print(
        f"B_rearmed_control {new_label} verdict={new_row['verdict']} bound={new_row['bound']} "
        f"judge_status={judged.get(new_label)} class_td={class_td} package_td={package_td} "
        f"in_residue={new_label in residue} PASS={arm_b}"
    )

    sites = inline_raises(FETCHERS, CLASS_NAME, "transform_data")
    fetcher = EquitySearchFetcher()
    params = EquitySearchQuery.model_validate({"keywords": "shopify"})
    string_reply = GraphqlReply(data={ROOT_FIELD: "not_a_list"})
    shape_kind, shape_code, shape_frame = refusal(
        lambda: fetcher.transform_data(string_reply, params)
    )
    direct_screen = refusal(lambda: tmx_client.list_rows("not_a_list"))
    arm_c = (
        sites == []
        and shape_kind == "TmxProviderError"
        and shape_code == "TMX_BAD_ROWS"
        and shape_frame.startswith("opendata.data.providers.tmx.models.client")
        and direct_screen[0] == "TmxProviderError"
        and direct_screen[1] == "TMX_BAD_ROWS"
    )
    print(
        f"C_no_dead_code inline_raise_lines={sites} via_mapper=({shape_kind},{shape_code},"
        f"{shape_frame}) via_client=({direct_screen[0]},{direct_screen[1]}) PASS={arm_c}"
    )

    entry = probe._entry("tmx", "EquitySearch")
    if entry is None:
        arm_d = False
        print(
            "D_tamper_bites witness_entry=MISSING for tmx::EquitySearch "
            "(no published tamper to apply) PASS=False"
        )
    else:
        document = entry["fixture_document"]()
        tampered = runner._apply_tamper(document, entry["tamper"])
        control_tamper = {
            "op": "set",
            "path": ["responses", 0, "body", "data", ROOT_FIELD, 0, "symbol"],
            "value": None,
        }
        untampered = runner._apply_tamper(document, control_tamper)
        tampered_reply = GraphqlReply(data=tampered["responses"][0]["body"]["data"])
        control_reply = GraphqlReply(data=untampered["responses"][0]["body"]["data"])
        tampered_face = refusal(lambda: fetcher.transform_data(tampered_reply, params))
        control_face = refusal(lambda: fetcher.transform_data(control_reply, params))
        # _apply_tamper deep-copies on its way out, so the published fixture must survive both
        # mutations and still serve its full row count after they have been applied.
        pristine_face = refusal(
            lambda: fetcher.transform_data(
                GraphqlReply(data=document["responses"][0]["body"]["data"]), params
            )
        )
        arm_d = (
            tampered_face[0] == entry["refusal_class"]
            and tampered_face[1] == entry["refusal_code"]
            and control_face[0] == "SERVED"
            and control_face[1] == "rows=2"
            and pristine_face == ("SERVED", "rows=2", "")
        )
        print(
            f"D_tamper_bites declared=({entry['refusal_class']},{entry['refusal_code']}) "
            f"op={entry['tamper']['op']} drop_symbol={tampered_face} "
            f"set_null_symbol={control_face} pristine_after={pristine_face} PASS={arm_d}"
        )

    controls = {
        "A_consumed_control": arm_a,
        "B_rearmed_control": arm_b,
        "C_no_dead_code": arm_c,
        "D_tamper_bites": arm_d,
    }
    gates = {
        "instrument_on_disk": INSTRUMENT.is_file(),
        "population_identity": len(judged) == len(report["models"]) > 0,
        "identity_set_is_pin": report["inventory"]["expected_identity_set_sha256"]
        == judge.EXPECTED_IDENTITY_SHA256,
        "labels_differ": new_label != OLD_CONTROL,
        "old_label_absent_from_residue": OLD_CONTROL not in residue,
        "new_label_present_in_residue": new_label in residue,
        "old_verdict_in_enum": str(old_row["verdict"]) in probe.VERDICTS,
        "new_verdict_in_enum": str(new_row["verdict"]) in probe.VERDICTS,
        "control_constant_is_new_label": new_label == probe.CONTROL_NEGATIVE,
    }
    print(
        f"CONTROLS_DIFF old={OLD_CONTROL}:{old_row['verdict']}/{judged.get(OLD_CONTROL)} "
        f"new={new_label}:{new_row['verdict']}/{judged.get(new_label)} "
        f"verdict_moved={str(old_row['verdict']) != 'NO_CODE_SITE'}"
    )
    verdict = all(controls.values()) and all(gates.values())
    print(
        f"rearm_face arms={len(controls)} arms_passed={sum(controls.values())} "
        f"gates={gates} all_pass={verdict}"
    )
    print(f"consumed_control_rearm_exit={0 if verdict else 1}")
    return 0 if verdict else 1


if __name__ == "__main__":
    raise SystemExit(main())
