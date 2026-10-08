"""Show that the per-model contract suite can fail.

One engine or declaration rule is broken at a time, the same nodes are re-run against the break, and
the bytes are restored. A green parametrized suite is not evidence on its own: it is evidence only
if there is a reading it would have taken had the code been wrong. The expected failing test ids are
stated *before* each leg runs, so a leg that passes while the code is broken is reported as a
falsification failure rather than a green run.

Each leg spawns a fresh interpreter for the same reason ``docs/evidence/C52/falsify_dwd_merge.py``
gives: in-process ``pytest.main`` keeps the first leg's bytecode in ``sys.modules``, so every later
mutation would be measured against someone else's code.

Nothing here touches a network, a warehouse or a live provider: the three files are offline suites
whose only transport is a synthetic one.
"""

from __future__ import annotations

import atexit
import hashlib
import subprocess  # nosec B404
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ENGINE = ROOT / "opendata/data/providers/_engine/http_json.py"
SPEC = ROOT / "opendata/data/providers/_engine/spec.py"
TESTING = ROOT / "opendata/data/providers/_engine/testing.py"

PYTEST_ARGV = ("-p", "no:cacheprovider", "--no-cov", "-q", "-m", "not e2e")
NODES = (
    "tests/test_provider_engine_contract.py",
    "tests/test_provider_model_contracts.py",
    "tests/test_cboe_engine_provider.py",
)

#: ``(label, file, exact source text, replacement text, test ids that must fail while it stands)``.
#: The last entry of each tuple names the reading this leg is calibrated to produce; an arm whose
#: expected id does not appear is this script's own bug report, not a passing test.
LEGS: tuple[tuple[str, Path, str, str, tuple[str, ...]], ...] = (
    (
        "D2  short page measured against the declared default instead of the size sent",
        ENGINE,
        "        page_size = effective_page_size(spec, params)",
        "        page_size = paging_page_size(spec) if spec.pagination.limit_key else 0",
        ("test_caller_set_page_size_survives_into_every_request",),
    ),
    (
        "D3  page totals summed rather than agreed on",
        ENGINE,
        "    return [total for total in (_declared_total(page, spec) for page in pages)"
        " if total is not None]",
        "    totals = [\n"
        "        total for total in (_declared_total(page, spec) for page in pages)"
        " if total is not None\n"
        "    ]\n"
        "    return [sum(totals)] if totals else []",
        (
            "test_pages_repeating_one_declared_total_read_complete",
            "test_declared_total_is_read_once_and_agrees_with_the_rows",
        ),
    ),
    (
        "Fixture scripted with identical records on every page (no row offset)",
        TESTING,
        "    records = [synthetic_record(spec, start + index) for index in range(count)]",
        "    records = [synthetic_record(spec, index) for index in range(count)]",
        ("test_typical_request_normalizes_the_declared_columns",),
    ),
    (
        "Declaration rule removed: page/offset accepted without an offset key",
        SPEC,
        '        if self.kind in {"offset", "page"} and not self.offset_key:\n'
        '            raise ValueError(f"{self.kind} pagination requires offset_key")',
        '        if self.kind in {"offset", "page"} and not self.limit_key:\n'
        '            raise ValueError(f"{self.kind} pagination requires a limit key")',
        ("test_paging_without_an_offset_key_is_refused",),
    ),
    (
        "Declaration rule removed: an optional path placeholder is accepted",
        SPEC,
        "            if not parameter.required and parameter.default is None:",
        "            if False and not parameter.required and parameter.default is None:",
        ("test_optional_path_placeholder_without_default_is_refused",),
    ),
    (
        "resolve_rows: an absent pointer reads as an empty result instead of a shape failure",
        ENGINE,
        '            raise ProviderEngineError(f"{spec.error_prefix}_SHAPE_INVALID")\n'
        "    if current is None:",
        "            return []\n    if current is None:",
        (
            # Only the faces that read a *missing record list*. A record lacking a declared column
            # key is refused by ``normalize_record``, one step later, so it still fails with the
            # pointer walk broken and is deliberately not expected here.
            "test_a_document_carrying_no_record_list_is_a_shape_failure",
        ),
    ),
)


def digest(path: Path) -> str:
    """Return the short sha256 of a file as it stands."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def run_suite() -> tuple[int, list[str], str]:
    """Run the offline nodes in a fresh interpreter; return rc, FAILED lines, the summary."""
    proc = subprocess.run(  # noqa: S603  # nosec B603  # literal argv, shell disabled
        [sys.executable, "-m", "pytest", *PYTEST_ARGV, *NODES],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    output = proc.stdout + proc.stderr
    failed = [line for line in output.splitlines() if line.startswith("FAILED ")]
    summary = [line for line in output.splitlines() if " passed" in line or " failed" in line]
    return int(proc.returncode), failed, summary[-1] if summary else "(no summary line)"


def main() -> int:
    """Run the unbroken control, then one broken leg at a time, restoring after each."""
    originals = {path: path.read_bytes() for _, path, _, _, _ in LEGS}
    pinned = {path: digest(path) for path in originals}

    def restore_all() -> None:
        for path, body in originals.items():
            path.write_bytes(body)

    atexit.register(restore_all)
    print(f"repo: {ROOT}")
    for path in sorted(originals):
        print(f"  pinned {path.relative_to(ROOT)} sha256={pinned[path]}")

    problems: list[str] = []

    rc, failed, summary = run_suite()
    print(f"\n[control] no tamper: rc={rc} failed_lines={len(failed)} :: {summary}")
    if rc != 0 or failed:
        problems.append("control leg is not green, so nothing below it is a clean comparison")

    for label, path, old, new, expected in LEGS:
        source = path.read_text(encoding="utf-8")
        hits = source.count(old)
        if hits != 1:
            problems.append(f"{label}: anchor found {hits} times in {path.name}, expected 1")
            print(f"\n[tamper] {label}\n  ANCHOR NOT UNIQUE ({hits}) — leg not run")
            continue
        path.write_text(source.replace(old, new), encoding="utf-8")
        try:
            rc, failed, summary = run_suite()
        finally:
            path.write_bytes(originals[path])
        matched = [item for item in expected if any(item in line for line in failed)]
        missing = [item for item in expected if item not in matched]
        print(f"\n[tamper] {label}")
        print(f"  rc={rc} failed_lines={len(failed)} :: {summary}")
        print(f"  expected failing ids matched={len(matched)}/{len(expected)} {matched}")
        extras = [line for line in failed if not any(item in line for item in expected)]
        for line in failed:
            print(f"  | {line}")
        if extras:
            print(
                f"  other failures while broken (also legitimate, listed for the record):"
                f" {len(extras)}"
            )
        if rc == 0 or not failed:
            problems.append(f"{label}: the suite stayed green while the rule was broken")
        if missing:
            problems.append(f"{label}: expected {missing} did not fail")
        if digest(path) != pinned[path]:
            problems.append(f"{label}: restore did not reproduce the pinned digest")

    rc, failed, summary = run_suite()
    print(f"\n[restored] rc={rc} failed_lines={len(failed)} :: {summary}")
    if rc != 0 or failed:
        problems.append("restored tree is not green")
    for path in sorted(originals):
        state = "IDENTICAL" if digest(path) == pinned[path] else "DIFFERENT"
        print(f"  restore {path.relative_to(ROOT)} sha256={digest(path)} {state}")

    print(f"\nself sha256 = {digest(Path(__file__))}")
    print(f"legs={len(LEGS)} problems={len(problems)}")
    for problem in problems:
        print(f"  !! {problem}")
    print("VERDICT: " + ("COUNTERFACTS HOLD" if not problems else "COUNTERFACTS DO NOT HOLD"))
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
