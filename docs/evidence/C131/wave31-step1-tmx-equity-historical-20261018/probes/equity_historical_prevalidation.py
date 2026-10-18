r"""Pre-validate the wave-31 tmx::EquityHistorical landing through the shipped runner paths.

Nothing here is evidence: fixtures land in a scratch dir outside the committed evidence root, and
every verdict comes from the repo's own judge -- the 25 phase runs are the real
``iter2_claims_probe_runner.py`` subprocesses the producer will spawn, and the control arms drive
the shipped ``_client_seam`` + ``_grant_context`` + ``fetch`` sequence, the same three calls
``_run_direct_with_seam`` makes. This row is the first tmx landing that needed a *guard* and not
only a ``row_model`` declaration, so the arms are about the guard's threshold rather than about the
seam shape wave 30 had to prove.

Arms, in the order they print:

* ``PHASE_TALLY`` -- 5 models x 5 phases, each in its own subprocess with sockets blocked, tallied
  by exit code (0 pass, 3 blocked, anything else fail). All five models run, not only the new one,
  because editing ``fetchers.py`` moves the source-pin digest every tmx row was signed under, so the
  four rows wave 30 landed have to re-serve from this generation or the merge drops them;
* ``CONTROL_FIVE_SERVED`` -- every witnessed row served through the seam, printing the row class the
  fetcher actually emitted against the class the domain resolves to, compared with ``is``. The four
  snapshot rows read ``emitted=[]``/``is_contract=False`` to the wave-28 residue instrument because
  that instrument reads construction through the AST and tmx builds its rows inside the ``_row``
  helper; the shipped direct leg already proves the identity by requiring
  ``type(row) is fetcher.row_model`` on every row;
* ``CONTROL_LIMIT_ON_THE_WIRE`` -- the ``limit`` the guard refuses against is the ``limit`` the
  fetcher actually posts, read out of the seam's own JSON payload, shown for two window widths so
  the threshold moves with the window instead of sitting at a constant;
* ``CONTROL_COUNT_GUARD_BITES`` -- the one-day control (1 row under limit 1) serves and the tamper
  (2 rows under limit 1) refuses at ``TMX_BAD_ROWS``;
* ``CONTROL_WINDOW_SCALING`` -- the *same* two-row document under the two-day window that asked for
  two rows serves both. Without this arm the guard would be indistinguishable from "refuse whenever
  TMX returns more than one row", and a count guard that cannot differ on its own threshold proves
  nothing;
* ``CONTROL_ADDED_ROW_IS_WELLFORMED`` -- the added prior-day record alone, under its own one-day
  window, maps and serves, so nothing about that row is wrong and only its count disagrees;
* ``CONTROL_TWO_BAD_ROWS_SITES`` -- a non-list root and an over-long list both refuse with
  ``TMX_BAD_ROWS`` from *different* frames, printed as file:line:function, so the module docstring's
  "a different site" claim is measured rather than asserted;
* ``CONTROL_TAMPER_BITES`` -- all five entries re-read: each declared tamper refuses with its
  declared code and its untampered control still serves.
"""

from __future__ import annotations

import importlib
import json
import subprocess  # nosec B404
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[5]
RUNNER_REL = "scripts/quality/iter2_claims_probe_runner.py"
sys.path.insert(0, str(REPO_ROOT / "scripts" / "quality"))
sys.path.insert(0, str(REPO_ROOT))

runner = importlib.import_module("iter2_claims_probe_runner")

MODELS = ("EquityQuote", "EquityInfo", "CompanyFilings", "PriceTargetConsensus", "EquityHistorical")
PHASES = ("implementation", "direct", "service", "contract", "counterfact")

ONE_DAY = {"symbol": "SHOP", "start_date": "2026-10-09", "end_date": "2026-10-09"}
TWO_DAYS = {"symbol": "SHOP", "start_date": "2026-10-08", "end_date": "2026-10-09"}
PRIOR_ONLY = {"symbol": "SHOP", "start_date": "2026-10-08", "end_date": "2026-10-08"}

#: Scratch evidence root, created at import so the printed face carries the real path.
SCRATCH = Path(tempfile.mkdtemp(prefix="w31s1-tmx-preval-"))


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
    """Run every phase of every witnessed tmx model as the producer's own subprocess."""
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


def _serve(model: str, document: dict[str, Any], query: dict[str, str]) -> tuple[list, Any]:
    """Drive the shipped direct sequence: seam patch, grant, fetch, seam guard."""
    from opendata.data.providers.tmx.models import fetchers as tmx_fetchers

    entry = _entry(model)
    fetcher = tmx_fetchers.EquityHistoricalFetcher()
    with runner._client_seam(entry, document) as seam:
        ctx = runner._grant_context(
            "tmx", model, entry["hosts"], sends=runner._carrier_sends(entry, document)
        )
        rows = list(fetcher.fetch(ctx=ctx, **query))
        runner._seam_guard(seam)
    return rows, seam


def _rows_of(document: dict[str, Any]) -> list[Any]:  # fixture rows stay dynamic
    payload: Any = document["responses"][0]["body"]["data"]
    return list(payload["getCompanyPriceHistory"])


def _history_document(rows: list[Any]) -> dict[str, Any]:
    """Wrap row dicts in the shipped envelope builder's own shape, one POST spec."""
    base = _entry("EquityHistorical")["fixture_document"]()
    document: dict[str, Any] = json.loads(json.dumps(base))
    document["responses"][0]["body"]["data"]["getCompanyPriceHistory"] = rows
    return document


def _two_row_document() -> dict[str, Any]:
    """The tampered fixture straight from the CONFIG, so no hand-authored second shape is used."""
    entry = _entry("EquityHistorical")
    tampered: dict[str, Any] = runner._apply_tamper(entry["fixture_document"](), entry["tamper"])
    return tampered


def _refusal_site(exc: BaseException) -> str:
    """The innermost TMX frame a refusal came from, so one code raised twice is told apart."""
    frames = [
        frame
        for frame in traceback.extract_tb(exc.__traceback__)
        if "providers/tmx" in str(frame.filename)
    ]
    if not frames:
        return "<no tmx frame>"
    last = frames[-1]
    return f"{Path(last.filename).name}:{last.lineno}:{last.name}"


def _refusal(exc: BaseException) -> str:
    return f"{type(exc).__name__} code={getattr(exc, 'code', None)} site={_refusal_site(exc)}"


def _refuse(document: dict[str, Any], query: dict[str, str]) -> str:
    """Serve a document that must not pass and return its refusal face, or NOT_REFUSED."""
    try:
        _serve("EquityHistorical", document, query)
    except Exception as exc:  # the refusal under test is whatever the provider ships
        return _refusal(exc)
    return "NOT_REFUSED"


def served_arm() -> None:
    """Print the row class each fetcher really emitted against the domain's contract class."""
    from opendata.data.domains import contract_model

    face: list[str] = []
    for model in MODELS:
        entry = _entry(model)
        document = entry["fixture_document"]()
        rows, seam, fetcher_type = runner._run_direct_with_seam("tmx", model, entry, document)
        contract = contract_model(fetcher_type.capability.domain)
        identity = all(type(row) is contract for row in rows)
        face.append(
            f"{model}:rows={len(rows)} emitted={sorted({type(r).__name__ for r in rows})} "
            f"contract={contract.__name__} runtime_identity={identity} "
            f"methods={sorted(set(seam.methods))}"
        )
    print("CONTROL_FIVE_SERVED " + " | ".join(face))


def _wire_read(query: dict[str, str], document: dict[str, Any]) -> str:
    """Serve one window through the shipped seam with an instance-level spy on the payload."""
    from opendata.data.providers.tmx.models import fetchers as tmx_fetchers

    entry = _entry("EquityHistorical")
    seen: dict[str, Any] = {}
    with runner._client_seam(entry, document) as seam:
        respond = seam._respond

        def spy(method: str, url: str, *, json_body: Any = None) -> Any:  # noqa: ANN401
            seen["variables"] = (json_body or {}).get("variables")
            return respond(method, url, json_body=json_body)

        seam._respond = spy  # instance spy; the shipped request() resolves self._respond
        ctx = runner._grant_context(
            "tmx",
            "EquityHistorical",
            entry["hosts"],
            sends=runner._carrier_sends(entry, document),
        )
        rows = list(tmx_fetchers.EquityHistoricalFetcher().fetch(ctx=ctx, **query))
        runner._seam_guard(seam)
    variables = seen["variables"] or {}
    params = tmx_fetchers.EquityHistoricalQuery.model_validate(query)
    return (
        f"{query['start_date']}..{query['end_date']}: wire_limit={variables.get('limit')} "
        f"_history_limit={tmx_fetchers._history_limit(params)} "
        f"rows_in_doc={len(_rows_of(document))} served={len(rows)} seam_calls={len(seam.calls)}"
    )


def wire_arm() -> None:
    """Read the ``limit`` off the payload the fetcher posted, for two window widths."""
    face = [
        _wire_read(ONE_DAY, _entry("EquityHistorical")["fixture_document"]()),
        _wire_read(TWO_DAYS, _two_row_document()),
    ]
    print("CONTROL_LIMIT_ON_THE_WIRE " + " | ".join(face))


def guard_arms() -> None:
    """Bite the guard on both sides and separate its refusal from ``list_rows``'."""
    one_row = _entry("EquityHistorical")["fixture_document"]()
    two_rows = _two_row_document()

    control_rows, _ = _serve("EquityHistorical", one_row, ONE_DAY)
    print(
        f"CONTROL_COUNT_GUARD_BITES control_rows={len(control_rows)} "
        f"tampered_refusal={_refuse(two_rows, ONE_DAY)} "
        f"declared_code={_entry('EquityHistorical')['refusal_code']}"
    )

    try:
        scaled, seam = _serve("EquityHistorical", two_rows, TWO_DAYS)
        dates = [str(row.date) for row in scaled]
        scaling = f"served={len(scaled)} dates={dates} seam_calls={len(seam.calls)}"
    except Exception as exc:  # this arm exists to catch exactly this reading
        scaling = f"REFUSED_UNEXPECTEDLY {_refusal(exc)}"
    print(f"CONTROL_WINDOW_SCALING two_rows_under_two_day_window {scaling}")

    lone = _history_document([_rows_of(two_rows)[1]])
    try:
        lone_rows, _ = _serve("EquityHistorical", lone, PRIOR_ONLY)
        lone_face = f"served={len(lone_rows)} date={lone_rows[0].date} close={lone_rows[0].close}"
    except Exception as exc:  # this arm exists to catch exactly this reading
        lone_face = f"REFUSED {_refusal(exc)}"
    print(f"CONTROL_ADDED_ROW_IS_WELLFORMED prior_day_alone {lone_face}")

    not_a_list = _history_document([{"datetime": "2026-10-09"}])
    root = "getCompanyPriceHistory"
    not_a_list["responses"][0]["body"]["data"][root] = {"datetime": "2026-10-09"}
    sites = [
        f"{label}: {_refuse(document, ONE_DAY)}"
        for label, document in (("non_list_root", not_a_list), ("over_limit_list", two_rows))
    ]
    frames = sorted({site.rsplit("site=", 1)[-1] for site in sites if "site=" in site})
    print(
        "CONTROL_TWO_BAD_ROWS_SITES " + " | ".join(sites) + f" distinct_frames={len(frames)} "
        f"frames={frames}"
    )


def tamper_arm() -> None:
    """Re-read every declared tamper in the module against its declared code."""
    codes: list[str] = []
    for model in MODELS:
        entry = _entry(model)
        control_rows, _, _ = runner._run_direct_with_seam(
            "tmx", model, entry, entry["fixture_document"]()
        )
        tampered = runner._apply_tamper(entry["fixture_document"](), entry["tamper"])
        try:
            runner._run_direct_with_seam("tmx", model, entry, tampered)
        except Exception as exc:  # the refusal under test is whatever the provider ships
            observed = getattr(exc, "code", f"<{type(exc).__name__}>")
        else:
            observed = "NOT_FALSIFIED"
        codes.append(
            f"{model}:control={len(control_rows)} declared={entry['refusal_code']} "
            f"observed={observed} match={observed == entry['refusal_code']}"
        )
    print("CONTROL_TAMPER_BITES " + " | ".join(codes))


def main() -> int:
    """Block sockets, run the 25 shipped phase verdicts, then print the control faces."""
    runner._block_real_sockets()  # the shipped judge does this first, so does the arm
    materialize()
    tally = run_phases()
    served_arm()
    wire_arm()
    guard_arms()
    tamper_arm()
    print(f"PREVALIDATION_DONE phase_failures_or_blocked={tally}")
    return 0 if tally == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
