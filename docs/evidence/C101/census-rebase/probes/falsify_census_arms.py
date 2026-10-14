"""Falsification arms for the re-based registry census pins.

Two-sided: a control run with no tamper must pass, each tamper must fail, and every file must
restore byte-exactly afterwards.
"""

import hashlib
import shutil
import subprocess  # nosec B404
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(".")
FIXTURE = Path("tests/fixtures/registry_batch5_snapshot.json")
TARGETS = [
    Path("tests/test_provider_domains.py"),
    Path("tests/test_pipeline_jobs.py"),
    Path("tests/test_alert_matrix.py"),
    Path("tests/test_alert_matrix_model_domains.py"),
]
BACKUP = Path("/tmp/census_arm_backup")  # nosec B108  # noqa: S108
BACKUP.mkdir(exist_ok=True)

TESTS = {
    "descriptor": (
        "tests/test_provider_domains.py::"
        "test_declared_domain_contracts_match_exact_registered_provider_models"
    ),
    "modeldomains": (
        "tests/test_alert_matrix_model_domains.py::"
        "test_default_scope_accounts_for_all_domains_legs_and_only_queries_legacy_tables"
    ),
    "sloos": (
        "tests/test_provider_domains.py::"
        "test_production_domains_preserve_v1_legacy_entries_and_explicit_semantics"
    ),
    "ods": (
        "tests/test_pipeline_jobs.py::TestPartitionMaintenanceJob::"
        "test_the_registered_census_reaches_the_ods_layer"
    ),
}


def snapshot_state() -> dict[Path, tuple[bytes, str]]:
    """Bytes and digest of every file the arms may touch, taken before the first write."""
    return {
        p: (p.read_bytes(), hashlib.sha256(p.read_bytes()).hexdigest()) for p in [FIXTURE, *TARGETS]
    }


def restore(state: dict[Path, tuple[bytes, str]]) -> None:
    """Put every file back from a snapshot_state() reading, byte for byte."""
    for p, (data, _h) in state.items():
        p.write_bytes(data)


def digest_of(path: Path) -> str:
    """SHA-256 of a file's current bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def re_pin(new_digest: str) -> None:
    """Rewrite the pre-tamper digest to ``new_digest`` in all four carrier modules."""
    for p in TARGETS:
        text = p.read_text(encoding="utf-8")
        for old in {digest_before}:
            text = text.replace(old, new_digest)
        p.write_text(text, encoding="utf-8")


def run(name: str) -> tuple[int, str]:
    """Run one census-pinning leg; return its exit code and its pytest reading line."""
    proc = subprocess.run(  # nosec B603  # noqa: S603
        [sys.executable, "-m", "pytest", "-q", "--no-cov", "-p", "no:cacheprovider", TESTS[name]],
        capture_output=True,
        text=True,
    )
    tail = [line for line in proc.stdout.splitlines() if "passed" in line or "failed" in line]
    return proc.returncode, (tail[-1] if tail else proc.stdout[-160:])


def tamper_fixture(mutate: Callable[[dict], None]) -> str:
    """Apply one mutation to the fixture and return the digest the tamper produced."""
    import json

    snap = json.loads(FIXTURE.read_text(encoding="utf-8"))
    mutate(snap)
    FIXTURE.write_text(json.dumps(snap, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return digest_of(FIXTURE)


state = snapshot_state()
for p in state:
    shutil.copyfile(p, BACKUP / p.name)
digest_before = state[FIXTURE][1]
results: list[tuple[str, str, int, str]] = []
try:
    # Control arm: untouched files must pass.
    results.extend(
        ("control", name, *run(name)) for name in ("descriptor", "modeldomains", "sloos", "ods")
    )

    arms = [
        # A: one qsem identity loses its descriptor eligibility -> descriptor census breaks.
        (
            "descriptor",
            lambda s: s["provider_model_identities"].__setitem__(
                next(
                    i
                    for i, r in enumerate(s["provider_model_identities"])
                    if r["source"] == "fred" and r["model"] == "SeniorLoanOfficerSurvey"
                ),
                {
                    **s["provider_model_identities"][
                        next(
                            i
                            for i, r in enumerate(s["provider_model_identities"])
                            if r["source"] == "fred" and r["model"] == "SeniorLoanOfficerSurvey"
                        )
                    ],
                    "canonical_ids_declared": False,
                },
            ),
            "A canonical_ids_declared flip on one qsem identity",
        ),
        # B: a legacy ths identity claims eligibility -> eligible set no longer equals descriptors.
        (
            "modeldomains",
            lambda s: s["provider_model_identities"].__setitem__(
                next(
                    i
                    for i, r in enumerate(s["provider_model_identities"])
                    if r["source"] == "ths" and r["model"] == "stock_daily"
                ),
                {
                    **s["provider_model_identities"][
                        next(
                            i
                            for i, r in enumerate(s["provider_model_identities"])
                            if r["source"] == "ths" and r["model"] == "stock_daily"
                        )
                    ],
                    "canonical_ids_declared": False,
                },
            ),
            "B canonical_ids_declared flip on the ths legacy identity",
        ),
        # C: scenario census rolled back one row -> the 342/29 attribution stops matching.
        (
            "sloos",
            lambda s: s["provider_model_identities"].__setitem__(
                next(
                    i
                    for i, r in enumerate(s["provider_model_identities"])
                    if r["domain"] == "senior_loan_officer_survey"
                ),
                {
                    **s["provider_model_identities"][
                        next(
                            i
                            for i, r in enumerate(s["provider_model_identities"])
                            if r["domain"] == "senior_loan_officer_survey"
                        )
                    ],
                    "scenario_present": False,
                },
            ),
            "C scenario_present rolled back on the SLOOS identity",
        ),
        # D: domain census back to the pre-wave value.
        (
            "ods",
            lambda s: s["counts"].__setitem__("domain_count", 337),
            "D domain_count back to 337",
        ),
    ]
    for target, mutate, label in arms:
        restore(state)
        digest_before = state[FIXTURE][1]
        new = tamper_fixture(mutate)
        re_pin(new)
        code, reading = run(target)
        results.append((label, target, code, reading))
finally:
    restore(state)

print("=== census pin falsification arms ===")
ok = True
for label, target, code, reading in results:
    expect_fail = not label.startswith("control")
    verdict = "FAIL" if code != 0 else "PASS"
    good = (code != 0) if expect_fail else (code == 0)
    ok = ok and good
    print(
        f"[{'as expected' if good else 'UNEXPECTED'}] {label} -> {target}: {verdict} :: {reading}"
    )

after = snapshot_state()
for p, (_d, h) in after.items():
    same = h == state[p][1]
    ok = ok and same
    print(f"[{'restored byte-exact' if same else 'RESTORE MISMATCH'}] {p} {h[:16]}")
print("ARMS_VERDICT:", "OK" if ok else "BROKEN")
