"""C103: copy this round's /tmp receipts into docs/evidence/C103/<family>/runs/ with headers.

Every archived carrier gets `carrier:` / `captured_at_local=` / `source_log=` / `argv=` /
`interpreter=` lines and one `<name>_exit=<digit>` reading, where the digit is either recorded
in the body itself, re-captured by a fresh re-run this turn, or written as
`not_captured_at_run_time` when the carrier holds only the command's own output. The two
whole-suite transcripts are archived as derived face files (FAILED census + summary +
collected header), because the raw logs stay in /tmp.

Usage: /Users/yunjinqi/opt/anaconda3/envs/py313/bin/python
         docs/evidence/C103/probes/carrier_archiver.py
"""

from __future__ import annotations

import hashlib
import subprocess  # nosec B404
from datetime import datetime
from pathlib import Path

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
EVID = REPO / "docs/evidence/C103"
PY = "/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python"
NOW = datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")

PROBES = "docs/evidence/C103"

# (family, carrier name, /tmp source, argv line, exit line)
VERBATIM: list[tuple[str, str, str, str, str]] = [
    (
        "fmp-fetch-array-arity",
        "arity-ab-abc.txt",
        "/tmp/c103_arity_ab.txt",  # nosec B108  # noqa: S108
        f"argv={PY} {PROBES}/fmp-fetch-array-arity/probes/arity_ab.py",
        "arity_ab_exit=0 (recorded in the body: 'arity_ab_exit=0' is the probe's own closing line)",
    ),
    (
        "batch5-source-pins",
        "pin-rebase-dry.txt",
        "/tmp/c103_rebase_dry.txt",  # nosec B108  # noqa: S108
        f"argv={PY} {PROBES}/batch5-source-pins/probes/pin_rebase.py",
        "pin_rebase_dry_exit=not_captured_at_run_time (verdict read from the body)",
    ),
    (
        "batch5-source-pins",
        "pin-rebase-apply.txt",
        "/tmp/c103_rebase_apply.txt",  # nosec B108  # noqa: S108
        f"argv={PY} {PROBES}/batch5-source-pins/probes/pin_rebase.py --apply",
        "pin_rebase_apply_exit=not_captured_at_run_time (verdict read from the body)",
    ),
    (
        "batch5-source-pins",
        "pin-rebase-idempotence-arm.txt",
        "/tmp/c103_pinrebase_dry2.txt",  # nosec B108  # noqa: S108
        f"argv={PY} {PROBES}/batch5-source-pins/probes/pin_rebase.py   (re-run after the "
        f"apply; must refuse)",
        "pin_rebase_dry_exit=1 (re-captured this turn)",
    ),
    (
        "batch5-source-pins",
        "pytest-selection-before.txt",
        "/tmp/c103_batch5_before.txt",  # nosec B108  # noqa: S108
        f"argv={PY} -m pytest tests/test_provider_domains.py tests/test_pipeline_jobs.py "
        "tests/test_alert_matrix.py tests/test_alert_matrix_model_domains.py -q --no-cov "
        "-p no:cacheprovider --tb=line",
        "pytest_selection_before_exit=not_captured_at_run_time (7 failed; pytest exits 1 on any "
        "collected failure)",
    ),
    (
        "batch5-source-pins",
        "pytest-selection-after-pins.txt",
        "/tmp/c103_batch5_after.txt",  # nosec B108  # noqa: S108
        f"argv={PY} -m pytest tests/test_provider_domains.py tests/test_pipeline_jobs.py "
        "tests/test_alert_matrix.py tests/test_alert_matrix_model_domains.py -q --no-cov "
        "-p no:cacheprovider --tb=line",
        "pytest_selection_after_pins_exit=not_captured_at_run_time (1 failed; pytest exits 1 "
        "on any "
        "collected failure)",
    ),
    (
        "batch5-scenario-census",
        "scenario-census.txt",
        "/tmp/c103_scenario_census.txt",  # nosec B108  # noqa: S108
        f"argv={PY} {PROBES}/batch5-scenario-census/probes/scenario_census.py",
        "scenario_census_exit=0 (recorded in the body)",
    ),
    (
        "batch5-scenario-census",
        "scenario-rebase-dry-1.txt",
        "/tmp/c103_scenario_dry.txt",  # nosec B108  # noqa: S108
        f"argv={PY} {PROBES}/batch5-scenario-census/probes/scenario_rebase.py",
        "scenario_rebase_dry_exit=not_captured_at_run_time (verdict read from the body)",
    ),
    (
        "batch5-scenario-census",
        "scenario-rebase-dry-2.txt",
        "/tmp/c103_scenario_dry2.txt",  # nosec B108  # noqa: S108
        f"argv={PY} {PROBES}/batch5-scenario-census/probes/scenario_rebase.py",
        "scenario_rebase_dry2_exit=not_captured_at_run_time (verdict read from the body)",
    ),
    (
        "batch5-scenario-census",
        "scenario-rebase-dry-3-shape-gate-refusal.txt",
        "/tmp/c103_scenario_dry3.txt",  # nosec B108  # noqa: S108
        f"argv={PY} {PROBES}/batch5-scenario-census/probes/scenario_rebase.py",
        "scenario_rebase_dry3_exit=not_captured_at_run_time (this is the instrument's own "
        "wrong-shape "
        "comparator being caught: live_counts.non_contract_exports=None while live measures 4)",
    ),
    (
        "batch5-scenario-census",
        "scenario-rebase-dry-4.txt",
        "/tmp/c103_scenario_dry4.txt",  # nosec B108  # noqa: S108
        f"argv={PY} {PROBES}/batch5-scenario-census/probes/scenario_rebase.py",
        "scenario_rebase_dry4_exit=not_captured_at_run_time (verdict read from the body)",
    ),
    (
        "batch5-scenario-census",
        "scenario-rebase-apply.txt",
        "/tmp/c103_scenario_apply.txt",  # nosec B108  # noqa: S108
        f"argv={PY} {PROBES}/batch5-scenario-census/probes/scenario_rebase.py --apply",
        "scenario_rebase_apply_exit=not_captured_at_run_time (verdict read from the body)",
    ),
    (
        "batch5-scenario-census",
        "scenario-rebase-apply-rerun.txt",
        "/tmp/c103_scenario_rerun.txt",  # nosec B108  # noqa: S108
        f"argv={PY} {PROBES}/batch5-scenario-census/probes/scenario_rebase.py --apply  "
        f"(second apply "
        "against the already-written fixture)",
        "scenario_rebase_rerun_exit=not_captured_at_run_time (verdict read from the body)",
    ),
    (
        "batch5-scenario-census",
        "scenario-rebase-idempotence-arm.txt",
        "/tmp/c103_rebase_dry2.txt",  # nosec B108  # noqa: S108
        f"argv={PY} {PROBES}/batch5-scenario-census/probes/scenario_rebase.py   (dry, after "
        f"the apply)",
        "scenario_rebase_dry_exit=1 (re-captured this turn)",
    ),
    (
        "batch5-scenario-census",
        "payload-literal-repin-dry.txt",
        "/tmp/c103_repin_dry.txt",  # nosec B108  # noqa: S108
        f"argv={PY} {PROBES}/batch5-scenario-census/probes/payload_literal_repin.py",
        "payload_literal_repin_dry_exit=not_captured_at_run_time (verdict read from the body)",
    ),
    (
        "batch5-scenario-census",
        "payload-literal-repin-apply.txt",
        "/tmp/c103_repin_apply.txt",  # nosec B108  # noqa: S108
        f"argv={PY} {PROBES}/batch5-scenario-census/probes/payload_literal_repin.py --apply",
        "payload_literal_repin_apply_exit=not_captured_at_run_time (verdict read from the body)",
    ),
    (
        "batch5-scenario-census",
        "payload-literal-repin-rerun.txt",
        "/tmp/c103_repin_rerun.txt",  # nosec B108  # noqa: S108
        f"argv={PY} {PROBES}/batch5-scenario-census/probes/payload_literal_repin.py --apply  "
        f"(second "
        "apply against the already-edited carriers)",
        "payload_literal_repin_rerun_exit=not_captured_at_run_time (verdict read from the body)",
    ),
    (
        "batch5-scenario-census",
        "payload-literal-idempotence-arm.txt",
        "/tmp/c103_repin_dry2.txt",  # nosec B108  # noqa: S108
        f"argv={PY} {PROBES}/batch5-scenario-census/probes/payload_literal_repin.py   (dry, "
        f"after the "
        "apply: the re-pinned digest is now recorded by the fixture itself, so 0 candidates "
        "remain)",
        "payload_literal_repin_dry_exit=1 (re-captured this turn)",
    ),
    (
        "batch5-scenario-census",
        "scenario-counterfact-first-pass.txt",
        "/tmp/c103_scenario_counterfact.txt",  # nosec B108  # noqa: S108
        f"argv={PY} {PROBES}/batch5-scenario-census/probes/scenario_counterfact.py",
        "scenario_counterfact_first_exit=not_captured_at_run_time (verdict read from the body)",
    ),
    (
        "batch5-scenario-census",
        "scenario-counterfact-reread.txt",
        "/tmp/c103_counterfact2.txt",  # nosec B108  # noqa: S108
        f"argv={PY} {PROBES}/batch5-scenario-census/probes/scenario_counterfact.py",
        "scenario_counterfact_exit=0 (re-captured this turn)",
    ),
    (
        "batch5-scenario-census",
        "pytest-selection-after-scenario-rebase.txt",
        "/tmp/c103_batch5_after_scenario.txt",  # nosec B108  # noqa: S108
        f"argv={PY} -m pytest tests/test_provider_domains.py tests/test_pipeline_jobs.py "
        "tests/test_alert_matrix.py tests/test_alert_matrix_model_domains.py -q --no-cov "
        "-p no:cacheprovider --tb=line",
        "pytest_selection_after_scenario_exit=not_captured_at_run_time (117 passed; pytest exits 0 "
        "when every collected item passes)",
    ),
    (
        "batch5-scenario-census",
        "pytest-selection-117-reread-this-turn.txt",
        "/tmp/c103_c117_selection.txt",  # nosec B108  # noqa: S108
        "argv=(printed in the body header) -- this is the run that PROVES the argv "
        "attribution above: "
        "the reconstructed four-file selection collects exactly 117 items, the same "
        "denominator the "
        "three archived pytest receipts print (7+110, 1+116, 0+117)",
        "c117_selection_exit=0 (re-captured this turn)",
    ),
    (
        "batch5-scenario-census",
        "guard-two-file-reread.txt",
        "/tmp/c103_guard_rerun.txt",  # nosec B108  # noqa: S108
        "argv=(printed in the body header)",
        "guard_rerun_exit=0 (re-captured this turn)",
    ),
]


def derive_suite_face(src: Path) -> str:
    """FAILED census + collected header + closing summary, never the progress bars."""
    lines = src.read_text(encoding="utf-8", errors="replace").splitlines()
    failed = [line for line in lines if line.startswith("FAILED")]
    collected = [line for line in lines if line.startswith("collected ")]
    summary = [
        line for line in lines if " passed" in line and "===" in line and "FAILED" not in line
    ]
    marks = [line for line in lines if "_exit=" in line]
    out = [
        "face: failed_lines=" + str(len(failed)),
        "face: collected_header=" + (" ".join(collected) if collected else "absent"),
        "face: summary_line=" + (summary[-1].strip() if summary else "absent"),
        "face: exit_markers_in_body=" + " ".join(marks),
        "identity: failed + passed + skipped must equal the collected header",
        "----------------------------------------------------------------------",
        *failed,
    ]
    return "\n".join(out) + "\n"


def main() -> int:
    """Copy each named receipt into its family's ``runs/`` with traceability headers."""
    written: list[tuple[Path, int]] = []
    missing: list[str] = []
    for family, name, src, argv, exit_line in VERBATIM:
        source = Path(src)
        if not source.is_file():
            missing.append(src)
            continue
        runs = EVID / family / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        body = source.read_text(encoding="utf-8", errors="replace")
        header = (
            f"carrier: {name}\n"
            f"round: C103\n"
            f"captured_at_local={NOW}\n"
            f"source_log={src}\n"
            f"source_bytes={len(body.encode('utf-8'))}\n"
            f"source_sha256_16={hashlib.sha256(body.encode('utf-8')).hexdigest()[:16]}\n"
            f"{argv}\n"
            f"interpreter={PY}\n"
            f"{exit_line}\n" + "-" * 70 + "\n"
        )
        dest = runs / name
        dest.write_text(header + body, encoding="utf-8")
        written.append((dest, dest.stat().st_size))

    for family, name, src in (
        (
            "batch5-scenario-census",
            "whole-suite-before-56-faces-derived.txt",
            "/tmp/suite_rerun_4.log",  # nosec B108  # noqa: S108
        ),
        (
            "batch5-scenario-census",
            "whole-suite-after-39-faces-derived.txt",
            "/tmp/c103_suite_after_batch5.txt",  # nosec B108  # noqa: S108
        ),
    ):
        source = Path(src)
        if not source.is_file():
            missing.append(src)
            continue
        runs = EVID / family / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        raw = source.read_text(encoding="utf-8", errors="replace")
        face = derive_suite_face(source)
        dest = runs / name
        dest.write_text(
            f"carrier: {name}\n"
            f"round: C103\n"
            f"captured_at_local={NOW}\n"
            f"derivation=FAILED census + collected header + closing summary only; the raw "
            f"transcript "
            f"(bytes={len(raw.encode('utf-8'))}) stays in {src} because whole-tree logs are "
            f"not committed\n"
            f"source_sha256_16={hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]}\n"
            f"interpreter={PY}\n"
            "whole_suite_exit=1 (both readings were red; the after-reading carries its own "
            "SUITE_EXIT=1 line in the body)\n" + "-" * 70 + "\n" + face,
            encoding="utf-8",
        )
        written.append((dest, dest.stat().st_size))

    for line in missing:
        print(f"face: MISSING_SOURCE {line}")
    print(f"face: sources_missing={len(missing)}")
    print(f"face: carriers_written={len(written)}")
    print(f"face: bytes_written={sum(size for _path, size in written)}")
    for path, size in sorted(written):
        rel = path.relative_to(REPO)
        print(f"file: {rel} bytes={size}")
    rc = subprocess.run(  # nosec B603 B607
        ["git", "-c", "core.quotepath=off", "status", "--porcelain", "--", "docs/evidence/C103"],  # nosec B404  # noqa: S607
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    print(f"face: git_status_lines={len([line for line in rc.splitlines() if line.strip()])}")
    # A carrier that says nothing (zero bytes) is not evidence.
    zero = [str(p.relative_to(REPO)) for p, size in written if size == 0]
    print(f"face: zero_byte_carriers={len(zero)} {zero}")
    # The bug this instrument just shipped with: a missing '+' turned the separator into
    # `HEADER * 70`. A carrier whose header block repeats is a corrupt archive, not evidence, so the
    # count of `carrier:` markers and the size relation to the source are both gated.
    repeated = []
    oversized = []
    for path, size in written:
        text = path.read_text(encoding="utf-8")
        if text.count("carrier: ") != 1:
            repeated.append(f"{path.name}={text.count('carrier: ')}")
        expected = [row[2] for row in VERBATIM if row[1] == path.name]
        if expected:
            source_bytes = Path(expected[0]).stat().st_size
            if size > source_bytes + 2048:
                oversized.append(f"{path.name} bytes={size} source={source_bytes}")
    print(f"face: header_repetitions={len(repeated)} {repeated}")
    print(f"face: oversized_vs_source={len(oversized)} {oversized}")
    if missing or zero or repeated or oversized:
        print(
            "verdict: REFUSED - a source was absent, or a carrier came out empty, header-repeated "
            "or larger than its source plus its header"
        )
        return 1
    print("verdict: PASS - every C103 receipt has a headered carrier under docs/evidence/C103")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
