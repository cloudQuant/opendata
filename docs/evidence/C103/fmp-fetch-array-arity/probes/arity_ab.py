"""C103 counterfact arms for the stale 3-argument ``fetch_array`` fake.

The product calls ``fetch_array(endpoint, params, ctx.timeout, self.http_transport)`` at
``opendata/data/providers/fmp/models/iter2_financials.py:161`` and ``:345``, while HEAD's copy of
``tests/test_fmp_iter2_market.py`` patched in a three-parameter fake.  ``provider_model_query`` maps
every exception to a typed upstream error (``except Exception: raise ... from None``), so the arity
drift surfaced as a generic upstream refusal rather than as a ``TypeError``.

Arms:
  A  HEAD's file bytes, materialized as a *separate* test module so the tracked file is never
     overwritten -> must fail (the contrast arm has to be able to differ).
  B  the repaired tracked file -> must pass with the same collected denominator.
  C  ``inspect`` the product signature and call 3- vs 4-parameter fakes with the four arguments
     the product actually passes.

Usage: python docs/evidence/C103/fmp-fetch-array-arity/probes/arity_ab.py [--keep-arm-a]
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import re
import subprocess  # nosec B404
import sys
from pathlib import Path
from typing import Any

REL = "tests/test_fmp_iter2_market.py"
ARM_A_REL = "tests/test_c103_fmp_head_copy.py"
FAILED = re.compile(r"^(?P<count>\d+) failed", re.MULTILINE)
PASSED = re.compile(r"^(?P<count>\d+) passed", re.MULTILINE)
COLLECTED = re.compile(r"collected (?P<count>\d+) items")
SUMMARY_TOKEN = re.compile(
    r"(?P<count>\d+) "
    r"(?P<kind>failed|passed|skipped|errors|error|xfail|xfails|xpass|xpases|deselected)"
)
KEEP_ARM_A = "--keep-arm-a" in sys.argv


def repo_root() -> Path:
    """Resolve the repository root by walking up to the nearest ``pytest.ini``."""
    current = Path(__file__).resolve()
    for ancestor in current.parents:
        if (ancestor / "pytest.ini").is_file():
            return ancestor
    raise SystemExit(f"no pytest.ini found above {current}")


REPO = repo_root()


def run_pytest(target: str) -> tuple[int, str]:
    """Run one pytest target offline and return its exit code and stdout."""
    proc = subprocess.run(  # nosec B603  # noqa: S603
        [sys.executable, "-m", "pytest", "-q", "--no-cov", "-p", "no:cacheprovider", target],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stdout + proc.stderr


def counts(text: str) -> dict[str, int]:
    r"""Parse pytest's result counts.

    The counts live on the closing ``=== N failed, M passed in ... ===`` line, so a ``^\\d+`` anchor
    never matches them; and the parts are summed across every token-bearing line so a run that also
    printed a warning count cannot silently lose a bucket.
    """
    out = {"collected": -1, "failed": 0, "passed": 0, "skipped": 0, "errors": 0, "other": 0}
    collected = COLLECTED.search(text)
    if collected:
        out["collected"] = int(collected.group("count"))
    for line in text.splitlines():
        for token in SUMMARY_TOKEN.finditer(line):
            kind = token.group("kind")
            value = int(token.group("count"))
            if kind in ("failed", "passed", "skipped"):
                out[kind] += value
            elif kind in ("errors", "error"):
                out["errors"] += value
            else:
                out["other"] += value
    out["accounted"] = out["failed"] + out["passed"] + out["skipped"] + out["errors"]
    out["balanced"] = int(out["collected"] == out["accounted"])
    return out


def fake_params(tree: ast.Module) -> list[int]:
    """Collect the parameter counts of every ``fake_fetch_array`` definition."""
    return [
        len(node.args.args) + len(node.args.kwonlyargs)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "fake_fetch_array"
    ]


def failure_lines(text: str) -> list[str]:
    """Collect the distinct error lines in a pytest tail, excluding harness paths."""
    unique = {
        line.strip()[:180]
        for line in text.splitlines()
        if "Error" in line and "test_c103" not in line and "tests/" not in line
    }
    return sorted(unique)[:5]


def arms_a_and_b() -> int:
    """Run the HEAD copy and the worktree copy of the module as the two contrast arms."""
    head_bytes = subprocess.run(  # nosec B603 B607  # noqa: S603
        ["git", "show", f"HEAD:{REL}"],  # nosec B404  # noqa: S607
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    tracked_bytes = (REPO / REL).read_text()
    if head_bytes == tracked_bytes:
        print("verdict: REFUSED - HEAD and worktree copies are identical, arm A cannot differ")
        return 1

    print(f"face: head_fake_params={fake_params(ast.parse(head_bytes))}")
    print(f"face: worktree_fake_params={fake_params(ast.parse(tracked_bytes))}")

    arm_a_path = REPO / ARM_A_REL
    try:
        arm_a_path.write_text(head_bytes)
        a_exit, a_text = run_pytest(ARM_A_REL)
        b_exit, b_text = run_pytest(REL)
    finally:
        if arm_a_path.exists() and not KEEP_ARM_A:
            arm_a_path.unlink()
        restored = (REPO / REL).read_text()
        print(
            "face: tracked_file_untouched="
            f"{restored == tracked_bytes} "
            f"sha256_16={hashlib.sha256(restored.encode()).hexdigest()[:16]}"
        )

    a, b = counts(a_text), counts(b_text)
    print(f"face: armA_head_copy {a} exit={a_exit}")
    print(f"face: armB_worktree {b} exit={b_exit}")
    print(f"face: denominator_matched={a['collected'] == b['collected']}")
    print(f"face: armA_failure_lines={failure_lines(a_text)}")

    if a["collected"] != b["collected"]:
        print("verdict: REFUSED - the arms collected different denominators, not comparable")
        return 1
    if not a["balanced"] or not b["balanced"]:
        print("verdict: REFUSED - an arm's buckets do not sum to its collected count")
        return 1
    if a["failed"] == 0 or a_exit == 0:
        print("verdict: REFUSED - arm A did not fail, the contrast arm proves nothing")
        return 1
    if b["failed"] or b["passed"] != b["collected"] or b_exit:
        print("verdict: REFUSED - arm B is not green")
        return 1
    print(f"verdict: PASS - arm A fails {a['failed']}, arm B passes {b['passed']}/{b['collected']}")
    return 0


def arm_c() -> int:
    """Replay the product call against a 3-parameter fake, then the working one."""
    sys.path.insert(0, str(REPO))
    from opendata.data.providers.fmp.models import iter2_financials
    from opendata.data.providers.fmp.models._client import fetch_array

    print(f"face: product_fetch_array_params={list(inspect.signature(fetch_array).parameters)}")

    def three_param_fake(endpoint: str, params: dict[str, str], timeout: float | None) -> list[Any]:
        return []

    def four_param_fake(
        endpoint: str,
        params: dict[str, str],
        timeout: float | None,
        transport: Any = None,  # noqa: ANN401
    ) -> list[Any]:
        return []

    args: tuple[Any, ...] = (
        "income-statement",
        {"symbol": "AAPL", "period": "annual", "limit": "5"},
        1.0,
        iter2_financials.IncomeStatementFetcher.http_transport,
    )
    print(f"face: product_positional_arg_count={len(args)}")

    results: dict[str, str] = {}
    for fake in (three_param_fake, four_param_fake):
        arity = len(inspect.signature(fake).parameters)
        try:
            fake(*args)
            results[f"{fake.__name__}[{arity}]"] = "accepted"
        except TypeError as exc:
            results[f"{fake.__name__}[{arity}]"] = f"TypeError({exc})"
    for name, outcome in results.items():
        print(f"face: armC_{name}={outcome}")

    refused = next(v for k, v in results.items() if k.endswith("[3]"))
    accepted = next(v for k, v in results.items() if k.endswith("[4]"))
    if not refused.startswith("TypeError") or accepted != "accepted":
        print("verdict: arm C REFUSED - the arity arms did not differ as expected")
        return 1
    print("verdict: arm C PASS - a 3-parameter fake cannot serve the 4-argument product call")
    return 0


def main() -> int:
    """Run all three arms and report the family verdict."""
    print(f"face: repo_root={REPO}")
    print(f"face: argv={sys.argv[1:]}")
    ab_exit = arms_a_and_b()
    c_exit = arm_c()
    arity_ab_exit = 0 if (ab_exit == 0 and c_exit == 0) else 1
    print(f"arms_a_b_exit={ab_exit} arm_c_exit={c_exit}")
    print(f"arity_ab_exit={arity_ab_exit}")
    return arity_ab_exit


if __name__ == "__main__":
    raise SystemExit(main())
