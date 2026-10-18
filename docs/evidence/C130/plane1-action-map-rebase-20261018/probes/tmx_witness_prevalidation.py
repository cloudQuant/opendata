r"""Pre-validate tmx's first witness module through the shipped runner paths.

Nothing here is evidence: fixtures land in a scratch dir outside the committed evidence root, and
every verdict comes from the repo's own judge -- the 20 phase runs are the real
``iter2_claims_probe_runner.py`` subprocesses the producer would spawn, and the control arms call
``_run_direct_with_seam`` from the same module. The controls exist because a hand-authored fixture
that *looks* offline can still prove nothing: a spec whose ``method`` key the fake never read, a
route needle nothing matches, and an envelope keyed by the operation name rather than its alias
would all produce a green-looking leg while the seam silently served something else. Each arm below
prints the face that refuses, and the arms that are supposed to be able to differ say what their
non-zero reading is.

Arms, in the order they print:

* ``PHASE_TALLY`` -- 4 models x 5 phases, each in its own subprocess with sockets blocked, tallied
  by exit code (0 pass, 3 blocked, anything else fail) with the runner's own ``VERDICT=`` line;
* ``CONTROL_FOUR_SERVED`` -- every witnessed row served through the seam, printing the row class the
  fetcher actually emitted next to the class the contract resolves to, because a declaration that
  pointed at a look-alike would pass ``direct`` and refuse on ``service``;
* ``CONTROL_VERB_GUARD`` -- flip the declared ``method`` to ``GET`` and the same fixture refuses:
  the key is load-bearing, not decorative, and a POST-shaped fixture cannot be laundered into a
  GET;
* ``CONTROL_ROUTE_GUARD`` -- move the ``url_contains`` needle off TMX's GraphQL route and the leg
  refuses: the seam is matched by the URL the fetcher really asks for, not served unconditionally;
  and because both guards above reach the provider as one sanitized ``TMX_TRANSPORT_ERROR``,
  ``CONTROL_SEAM_VERB_DETAIL`` / ``CONTROL_SEAM_ROUTE_DETAIL`` re-read the same two mutations at the
  seam itself, so a verb mismatch and a route miss do not collapse into one reading;
* ``CONTROL_ROOT_ALIAS_GUARD`` -- re-key CompanyFilings' body by its *operation* name
  (``getCompanyFilings``) instead of its selection alias (``filings``) and it refuses at
  ``TMX_BAD_ENVELOPE``: the alias-vs-name distinction the module docstring claims is the difference
  between a fixture that maps and one that never reaches a mapper;
* ``CONTROL_TAMPER_BITES`` -- per model, the untampered control serves and the declared tamper
  refuses with the declared code, so the counterfact's exact-shape match is measured rather than
  asserted, and the four codes are shown to be four different bites (three ``TMX_BAD_ROW`` cells and
  the fetcher's own ``TMX_BAD_ROWS`` count guard) rather than one refusal reached four ways;
* ``CONTROL_VALUE_NOT_PRESENCE`` -- write the tampered cell back as the JSON number TMX publishes
  and the leg serves again: the refusal is caused by the value's type, not by the path having been
  touched, which is what makes the counterfact a statement about ``last_price`` at all.
"""

from __future__ import annotations

import importlib
import json
import subprocess  # nosec B404
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[5]
RUNNER_REL = "scripts/quality/iter2_claims_probe_runner.py"
sys.path.insert(0, str(REPO_ROOT / "scripts/quality"))
sys.path.insert(0, str(REPO_ROOT))

runner = importlib.import_module("iter2_claims_probe_runner")

MODELS = ("EquityQuote", "EquityInfo", "CompanyFilings", "PriceTargetConsensus")
PHASES = ("implementation", "direct", "service", "contract", "counterfact")

#: The scratch evidence root is created by main() so the printed face carries the real path.
SCRATCH = Path(tempfile.mkdtemp(prefix="w30-tmx-preval-"))


def materialize() -> None:
    """Write each fixture the way the producer does: shipped builder, shipped serialization."""
    fixtures = SCRATCH / "fixtures" / "tmx"
    fixtures.mkdir(parents=True, exist_ok=True)
    models = runner.witnesses.PROVIDER_WITNESSES["tmx"]["models"]
    for model in MODELS:
        builder = models[model]["fixture_document"]
        payload = json.dumps(builder(), indent=2, ensure_ascii=False, sort_keys=True) + "\n"
        (fixtures / f"{model}.json").write_text(payload, encoding="utf-8")
    print(f"materialized: fixtures={len(MODELS)} root={SCRATCH}")


def run_phases() -> int:
    """Run every phase of every model as the producer's own subprocess and tally the verdicts."""
    passed = failed = blocked = 0
    for model in MODELS:
        for phase in PHASES:
            completed = subprocess.run(  # noqa: S603  # nosec B603, B607  # argv is the shipped runner
                [
                    sys.executable,
                    str(REPO_ROOT / RUNNER_REL),
                    "--provider",
                    "tmx",
                    "--model",
                    model,
                    "--phase",
                    phase,
                    "--evidence-root",
                    str(SCRATCH),
                ],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
                env={"PYTHONPATH": str(REPO_ROOT), "PATH": "/usr/bin:/bin"},
            )
            verdict = next(
                (line for line in completed.stdout.splitlines() if line.startswith("VERDICT=")), "?"
            )
            if completed.returncode == 0:
                passed += 1
            elif completed.returncode == 3:
                blocked += 1
            else:
                failed += 1
            print(f"phase: tmx::{model}::{phase} exit={completed.returncode} {verdict}")
            if completed.returncode != 0:
                print(f"   stderr_tail={completed.stderr.strip().splitlines()[-3:]}")
    total = len(MODELS) * len(PHASES)
    print(f"PHASE_TALLY passed={passed} failed={failed} blocked={blocked} total={total}")
    return failed + blocked


def _entry(model: str) -> dict[str, Any]:
    entry = dict(runner.witnesses.model_entry("tmx", model))
    entry["hosts"] = runner.witnesses.provider_config("tmx")["allowed_hosts"]
    return entry


def _fetch(model: str, document: dict[str, Any] | None = None) -> tuple[list, Any, Any]:
    entry = _entry(model)
    return runner._run_direct_with_seam(
        "tmx", model, entry, document if document is not None else entry["fixture_document"]()
    )


def _mutated(model: str, path: list[Any], value: Any) -> dict[str, Any]:  # noqa: ANN401  # fixture cell
    """Rewrite one key of a freshly built fixture, the way ``_apply_tamper`` would."""
    document = json.loads(json.dumps(_entry(model)["fixture_document"]()))
    cursor: Any = document
    for step in path[:-1]:
        cursor = cursor[step]
    cursor[path[-1]] = value
    return document


def controls() -> None:
    """Show the faces the fixture has to refuse, beside the ones that serve."""
    from opendata.data.domains import contract_model

    # arm 1: all four rows served, with the emitted class checked against the resolved contract.
    served: list[str] = []
    for model in MODELS:
        rows, seam, fetcher_type = _fetch(model)
        emitted = sorted({type(row).__name__ for row in rows})
        domain = fetcher_type.capability.domain
        is_contract = all(type(row) is contract_model(domain) for row in rows)
        served.append(
            f"{model}:rows={len(rows)} emitted={emitted} is_contract={is_contract} "
            f"methods={sorted(set(seam.methods))}"
        )
    print("CONTROL_FOUR_SERVED " + " | ".join(served))

    # arm 2: the method key is load-bearing -- a POST route cannot be served by a GET-shaped spec.
    flipped = _mutated("EquityQuote", ["responses", 0, "method"], "GET")
    try:
        _fetch("EquityQuote", flipped)
    except Exception as exc:
        print(f"CONTROL_VERB_GUARD refused: {type(exc).__name__} {exc}")
    else:
        print("CONTROL_VERB_GUARD NOT_FALSIFIED (method key is decorative)")

    # arm 3: the needle must be the route the fetcher really posts to, or nothing is served.
    offroute = _mutated("EquityQuote", ["responses", 0, "url_contains"], "app-money.tmx.com/other")
    try:
        _fetch("EquityQuote", offroute)
    except Exception as exc:
        print(f"CONTROL_ROUTE_GUARD refused: {type(exc).__name__} {exc}")
    else:
        print("CONTROL_ROUTE_GUARD NOT_FALSIFIED (the seam ignores the route it declares)")

    # 3b: both guards above are seen by the provider as one sanitized TMX_TRANSPORT_ERROR, which is
    # two causes for one code. The seam's own message is read at the seam so the arms say WHICH half
    # refused -- a verb mismatch and a route miss must not collapse into the same reading.
    spec = dict(_entry("EquityQuote")["fixture_document"]()["responses"][0])
    url = "https://app-money.tmx.com/graphql"
    get_only = runner._FakeSharedClient([{**spec, "method": "GET"}])
    try:
        get_only.request("POST", url, json_body={"operationName": "getQuoteBySymbol"})
    except runner.WitnessFailure as exc:
        print(f"CONTROL_SEAM_VERB_DETAIL refused: {exc}")
    else:
        print("CONTROL_SEAM_VERB_DETAIL NOT_FALSIFIED")
    elsewhere = runner._FakeSharedClient([{**spec, "url_contains": "app-money.tmx.com/other"}])
    try:
        elsewhere.request("POST", url, json_body={"operationName": "getQuoteBySymbol"})
    except runner.WitnessFailure as exc:
        print(f"CONTROL_SEAM_ROUTE_DETAIL refused: {exc}")
    else:
        print("CONTROL_SEAM_ROUTE_DETAIL NOT_FALSIFIED")

    # arm 4: an envelope keyed by the operation name rather than by its selection alias never
    # reaches a mapper at all.
    miskeyed = _mutated(
        "CompanyFilings", ["responses", 0, "body", "data"], {"getCompanyFilings": []}
    )
    try:
        _fetch("CompanyFilings", miskeyed)
    except Exception as exc:
        code = getattr(exc, "code", "")
        print(f"CONTROL_ROOT_ALIAS_GUARD refused: {type(exc).__name__} code={code} {exc}")
    else:
        print("CONTROL_ROOT_ALIAS_GUARD NOT_FALSIFIED (alias and operation name are the same cell)")

    # arm 5: each declared tamper bites with its declared code, and its control still serves.
    codes: list[str] = []
    for model in MODELS:
        tamper = _entry(model)["tamper"]
        declared = _entry(model)["refusal_code"]
        control_rows, _, _ = _fetch(model)
        tampered = _mutated(model, tamper["path"], tamper["value"])
        try:
            _fetch(model, tampered)
        except Exception as exc:
            observed = getattr(exc, "code", f"<{type(exc).__name__}>")
        else:
            observed = "NOT_FALSIFIED"
        codes.append(
            f"{model}:control={len(control_rows)} declared={declared} observed={observed} "
            f"match={observed == declared}"
        )
    print("CONTROL_TAMPER_BITES " + " | ".join(codes))

    # arm 6: writing the cell back as a JSON number serves again -- the refusal was the value's
    # type, not the fact that the path had been touched. Same path the CONFIG declares.
    price_path = list(_entry("EquityQuote")["tamper"]["path"])
    restored = _mutated("EquityQuote", price_path, 121.44)
    rows, seam, _ = _fetch("EquityQuote", restored)
    print(
        f"CONTROL_VALUE_NOT_PRESENCE served: rows={len(rows)} last_price={rows[0].last_price!r} "
        f"seam_calls={len(seam.calls)}"
    )


def main() -> int:
    """Block sockets, run the 20 shipped phase verdicts, then print the control faces."""
    runner._block_real_sockets()  # the shipped judge does this first, so does the arm
    materialize()
    tally = run_phases()
    controls()
    print(f"PREVALIDATION_DONE phase_failures_or_blocked={tally}")
    return 0 if tally == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
