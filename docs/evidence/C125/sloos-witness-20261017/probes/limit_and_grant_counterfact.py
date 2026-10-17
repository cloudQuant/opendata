"""Falsify the two things that made ``fred::SeniorLoanOfficerSurvey`` look cheap and was not.

The wave-19 forecast (§81.8) ranked this row first among "the two cheapest rows" using a
predicate that measured ``bound`` / ``row_model`` / ``witness``. All three held. What the
predicate never measured was the request-budget seam: this fetcher preflights its own fan-out
(``models/sloos.py:61`` ``_preflight_request_allowance``) against the grant the runner hands it,
and the shipped ``_grant_context`` default was 3 sends while the ``spreads`` category needs 6. The
row therefore refused ``implementation`` and every later phase *before* touching a fixture, and no
amount of shard authoring would have changed that.

Three arms, each with a value the previous state of the repo would read differently:

``GRANT``
    the carrier-derived grant the runner now computes from the fixture envelope. It must be 6,
    because the only honest source for "how many requests can this carrier answer" is the carrier.
``LEGACY_GRANT``
    the same carrier run with the pre-fix grant of 3. Expected: the fetcher's own preflight refuses
    with ``FRED_QUERY_LIMIT``. This is the positive control for the claim "the blocker was the
    grant, not the data" -- if the repo had been able to produce rows under 3 sends, this arm would
    print ``rows=12`` and the fix would be unnecessary.
``CONSTANT_LIMIT``
    the shipped carrier with every page header rewritten to the *first* leg's ``limit``. Expected:
    ``FRED_PAGINATION_ERROR``. A page whose echoed ``limit`` equals a constant is the shape a
    hand-written shard normally has, so this arm shows the header check at ``models/sloos.py:148``
    is a live guard and that the decreasing ``remaining_records`` accounting
    (``models/sloos.py:216,230``) is what the carrier must mirror.

Every arm runs through the runner's own private helpers (``_run_direct_with_seam``,
``_grant_context``, ``_client_seam``, ``_typed_provider_refusal``) and through the real fetcher
with sockets refused process-wide, so what is printed is the shipped code refusing, not a
re-implementation of it in the probe.

Run it from the repository root::

    /Users/yunjinqi/opt/anaconda3/envs/py313/bin/python \
        docs/evidence/C125/sloos-witness-20261017/probes/limit_and_grant_counterfact.py

Exit is 0 only when all three arms land on their expected readings.
"""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from typing import Any, Final

REPO_ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "quality"))

import iter2_claims_fixtures as witnesses  # noqa: E402  # the repo's own shard loader
import iter2_claims_probe_runner as runner  # noqa: E402  # the repo's own judge helpers

PROVIDER: Final = "fred"
MODEL: Final = "SeniorLoanOfficerSurvey"
FIXTURE: Final = Path("docs/evidence/C101/fixtures/fred/SeniorLoanOfficerSurvey.json")
EXPECTED_ROWS: Final = 12
EXPECTED_LEGS: Final = 6
LEGACY_SENDS: Final = 3


def _entry() -> dict[str, Any]:
    """Assemble the witness entry exactly the way the runner's ``main`` does.

    ``hosts`` is not a shard field: it comes from the provider config's ``allowed_hosts``, and the
    credential tokens are injected into ``os.environ`` before the fetcher runs. Reading only
    ``model_entry`` gave this probe a ``KeyError: 'hosts'``, which the first version then hid
    inside its own exception arm.
    """
    config = witnesses.provider_config(PROVIDER)
    entry = dict(witnesses.model_entry(PROVIDER, MODEL))
    entry["hosts"] = config["allowed_hosts"]
    for name, value in (config.get("credential_env") or {}).items():
        if not os.environ.get(name):
            os.environ[name] = value
    return entry


def _pages(document: dict[str, Any]) -> list[dict[str, Any]]:
    spec = document["responses"][0]
    return list(spec["sequence"])


def _run_with_grant(document: dict[str, Any], sends: int) -> tuple[int, int, str | None]:
    """Drive the real fetcher at an explicit grant; return rows, seam calls, refusal code."""
    entry = _entry()
    fetcher_type = runner._resolve_fetcher_type(PROVIDER, MODEL)
    ctx = runner._grant_context(PROVIDER, MODEL, entry["hosts"], sends=sends)
    try:
        with runner._client_seam(entry, document) as client:
            rows = list(fetcher_type().fetch(ctx=ctx, **entry["query"]))
            return len(rows), len(client.calls), None
    except Exception as exc:  # the refusal is the reading this arm is asking for
        code = runner._typed_provider_refusal(exc, PROVIDER)
        return 0, 0, code if code else f"UNtyped:{type(exc).__name__}"


def _run_derived(document: dict[str, Any]) -> tuple[int, int, str | None, int]:
    """Drive the fetcher through the runner's own carrier-derived grant."""
    entry = _entry()
    grant = runner._carrier_sends(entry, document)
    try:
        rows, client, _ = runner._run_direct_with_seam(PROVIDER, MODEL, entry, document)
        return len(rows), len(client.calls), None, grant
    except Exception as exc:
        code = runner._typed_provider_refusal(exc, PROVIDER)
        return 0, 0, (code if code else f"UNtyped:{type(exc).__name__}"), grant


def _constant_limit_carrier(document: dict[str, Any]) -> dict[str, Any]:
    """Rewrite every page header to the first leg's ``limit`` -- the naive hand-written shape."""
    mutated = copy.deepcopy(document)
    pages = _pages(mutated)
    constant = pages[0]["limit"]
    for page in pages:
        page["limit"] = constant
    return mutated


def main() -> int:
    """Run the three arms and refuse to exit 0 unless each lands on its expected reading."""
    runner._block_real_sockets()
    document = json.loads((REPO_ROOT / FIXTURE).read_text(encoding="utf-8"))
    limits = [str(page["limit"]) for page in _pages(document)]
    print(f"fixture={FIXTURE} pages={len(_pages(document))} limits={','.join(limits)}")

    # Each arm prints as it is measured: an instrument that dies in arm 2 must still show arm 1.
    grant_rows, grant_calls, grant_code, grant = _run_derived(document)
    print(
        f"GRANT derived={grant} expected={EXPECTED_LEGS} rows={grant_rows} "
        f"seam_requests={grant_calls} code={grant_code} expected_code=None"
    )

    legacy_rows, legacy_calls, legacy_code = _run_with_grant(document, LEGACY_SENDS)
    print(
        f"LEGACY_GRANT sends={LEGACY_SENDS} rows={legacy_rows} seam_requests={legacy_calls} "
        f"code={legacy_code} expected=FRED_QUERY_LIMIT"
    )

    const_rows, const_calls, const_code, const_grant = _run_derived(
        _constant_limit_carrier(document)
    )
    print(
        f"CONSTANT_LIMIT grant={const_grant} rows={const_rows} seam_requests={const_calls} "
        f"code={const_code} expected=FRED_PAGINATION_ERROR"
    )

    ok = (
        grant == EXPECTED_LEGS
        and grant_rows == EXPECTED_ROWS
        and grant_calls == EXPECTED_LEGS
        and grant_code is None
        and legacy_code == "FRED_QUERY_LIMIT"
        and legacy_rows == 0
        and legacy_calls == 0
        and const_code == "FRED_PAGINATION_ERROR"
        and const_rows == 0
        and len(set(limits)) == EXPECTED_LEGS
    )
    print(f"EXPECTED_LIMITS={','.join(limits)} distinct={len(set(limits)) == EXPECTED_LEGS}")
    print(f"LIMIT_AND_GRANT_PROVEN={ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
