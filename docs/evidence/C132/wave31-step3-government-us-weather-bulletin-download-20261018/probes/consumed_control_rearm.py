"""Re-arm the negative control that wave-31 step 3 consumed, and prove both readings in one run.

``docs/evidence/C128/.../incomplete_row_guard_check.py`` gated its arm ``B_landed_negative`` on
``CONTROL_NEGATIVE`` naming ``government_us::WeatherBulletinDownload`` -- a row this wave landed by
adding ``row_model`` plus the two post-response guards at
``opendata/data/providers/government_us/models/weather_bulletin_download.py:135`` and ``:137``. The
commit that lands the row also silently retires the arm that was written against that row, so the
re-base in that instrument is not a cosmetic edit and is not asserted here on trust: the classifier
is run over BOTH labels, in this generation, and the readings must differ.

Three arms, all of which can fail:

``A_consumed_control``
    the shipped classifier over the OLD label must now read a ``TRIPPABLE`` verdict, so the old
    ``PASS=True`` face is unreachable from the current tree. This is the red that the re-base is an
    answer to; the pre-landing green for the same label is the wave-31 step-2 re-take of that
    instrument (``wave31-step2-incomplete_row_guard_check.txt:72``,
    ``verdict=NO_CODE_SITE PASS=True``).
``B_rearmed_control``
    the same predicate over the NEW ``CONTROL_NEGATIVE`` must read non-trippable, i.e. the arm the
    re-base leaves behind is live rather than vacuous, and arm C of that instrument survives because
    the class-scoped ``transform_data`` count is 0 while the provider screen is not.
``C_attributable``
    the old label's movement must be attributable to the two guards this wave added, not to a
    classifier change: the same shipped ``_row`` reading over the landed row reports
    ``code_raises >= 2``, an ``extract_data`` site count of 2, both code-carrying raise lines inside
    the landed file, and ``judge_status=OFFLINE_ACCEPTED`` (the row left the residue population,
    which is the second, independent reason it could not stay the negative control).

The instrument is imported, never copied: the verdict comes from its own ``_row`` and ``TRIPPABLE``,
so a change in this probe cannot change what is being measured.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[5]
INSTRUMENT_DIR = REPO_ROOT / "docs/evidence/C128/plane1-residue-readiness-20261017/probes"
INSTRUMENT = INSTRUMENT_DIR / "incomplete_row_guard_check.py"
OLD_CONTROL = "government_us::WeatherBulletinDownload"
LANDED_FILE = (
    REPO_ROOT / "opendata/data/providers/government_us/models/weather_bulletin_download.py"
)

sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(INSTRUMENT_DIR))

import incomplete_row_guard_check as probe  # noqa: E402  # the shipped C128 classifier


def main() -> int:
    """Print the two readings side by side and gate on their differing in the current tree."""
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
    print(
        f"POPULATION report_rows={len(report['models'])} judged={len(judged)} "
        f"incomplete={sum(1 for s in judged.values() if s == 'INCOMPLETE')} "
        f"old_in={OLD_CONTROL in judged} new_in={probe.CONTROL_NEGATIVE in judged}"
    )

    old_row = probe._row(OLD_CONTROL, judged.get(OLD_CONTROL, "?"))
    old_trippable = str(old_row["verdict"]) in probe.TRIPPABLE
    print(
        f"A_consumed_control {OLD_CONTROL} verdict={old_row['verdict']} "
        f"judge_status={judged.get(OLD_CONTROL)} counts={old_row.get('counts')} "
        f"raises={old_row.get('raises')} code_raises={old_row.get('code_raises')} "
        f"trippable_now={old_trippable} old_arm_b_would_pass={not old_trippable} "
        f"PASS={old_trippable}"
    )

    new_label = str(probe.CONTROL_NEGATIVE)
    new_row = probe._row(new_label, judged.get(new_label, "?"))
    new_trippable = str(new_row["verdict"]) in probe.TRIPPABLE
    class_td = int(new_row.get("counts", {}).get("transform_data", 0))
    new_screen = probe._provider_screen(new_label.partition("::")[0])
    arm_b = (not new_trippable) and class_td == 0 and new_screen.get("transform_data", 0) > 0
    print(
        f"B_rearmed_control {new_label} verdict={new_row['verdict']} class_td={class_td} "
        f"package_td={new_screen.get('transform_data', 0)} trippable={new_trippable} PASS={arm_b}"
    )

    source = LANDED_FILE.read_text(encoding="utf-8")
    guard_lines = [
        number
        for number, line in enumerate(source.splitlines(), start=1)
        if "GovernmentUSProviderError(" in line
    ]
    extract_sites = int((old_row.get("counts") or {}).get("extract_data", 0))
    arm_c = (
        str(old_row["verdict"]) == "DOCUMENT_TRIPPED"
        and int(old_row.get("code_raises") or 0) >= 2
        and extract_sites == 2
        and len(guard_lines) == 2
        and judged.get(OLD_CONTROL) == "OFFLINE_ACCEPTED"
    )
    print(
        f"C_attributable {OLD_CONTROL} verdict={old_row['verdict']} "
        f"code_raises={old_row.get('code_raises')} extract_sites={extract_sites} "
        f"guard_raise_lines={guard_lines} judge_status={judged.get(OLD_CONTROL)} PASS={arm_c}"
    )

    differing = old_trippable != new_trippable
    controls = {
        "A_consumed_control": old_trippable,
        "B_rearmed_control": arm_b,
        "C_attributable": arm_c,
    }
    gates = {
        "instrument_on_disk": INSTRUMENT.is_file(),
        "population_identity": len(judged) == len(report["models"]) > 0,
        "labels_differ": new_label != OLD_CONTROL,
        "readings_differ": differing,
        "old_verdict_in_enum": str(old_row["verdict"]) in probe.VERDICTS,
        "new_verdict_in_enum": str(new_row["verdict"]) in probe.VERDICTS,
    }
    print(
        f"CONTROLS_DIFF old={OLD_CONTROL}:{old_row['verdict']} "
        f"new={new_label}:{new_row['verdict']} readings_differ={differing}"
    )
    verdict = all(controls.values()) and all(gates.values())
    print(
        f"rearm_face arms={len(controls)} arms_passed={sum(1 for v in controls.values() if v)} "
        f"gates={gates} all_pass={verdict}"
    )
    print(f"consumed_control_rearm_exit={0 if verdict else 1}")
    return 0 if verdict else 1


if __name__ == "__main__":
    raise SystemExit(main())
