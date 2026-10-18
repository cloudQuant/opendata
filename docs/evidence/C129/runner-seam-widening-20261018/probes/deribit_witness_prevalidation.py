r"""Pre-validate deribit's first witness module through the shipped runner paths.

Nothing here is evidence: fixtures land in a scratch dir outside the committed evidence root, and
every verdict comes from the repo's own judge -- the 25 phase runs are the real
``iter2_claims_probe_runner.py`` subprocesses the producer would spawn, and the control arms call
``_run_direct_with_seam`` from the same module. The controls exist because the wave paid for a
widened fake: an id that the fetcher never checks and a method key the fake never read would both
make the fixture look authoritative while proving nothing, so each arm shows the face that refuses.

Arms, in the order they print:

* ``PHASE_TALLY`` - 5 models x 5 phases, each in its own subprocess with sockets blocked, tallied by
  exit code (0 pass, 3 blocked, anything else fail) and with the runner's own ``VERDICT=`` line;
* ``CONTROL_POSITIVE`` - the POST-shaped, id-echoing fixture serves both chart calls of one fetch;
* ``CONTROL_SECOND_GENERATION`` - a second fetch in the same process is also served, which is the
  property that the fixed-id fixtures of every other provider never had to have and that the
  ``counterfact`` phase requires;
* ``CONTROL_ECHO_DECLARATION_GUARD`` - delete ``echo_request_id`` and the same fixture refuses: the
  ``id: null`` placeholders are not silently acceptable;
* ``CONTROL_VERB_GUARD`` - flip the declared ``method`` to ``GET`` and it refuses: the key is
  load-bearing, not decorative;
* ``CONTROL_SEQUENCE_EXHAUSTION`` - ask three instruments of a two-page fixture and the leg refuses;
* ``CONTROL_SEAM_PAGES`` / ``CONTROL_SEAM_EXHAUSTION`` - the same bound read at the seam itself,
  because ``client.py`` wraps any transport exception into ``DERIBIT_TRANSPORT_ERROR`` and would
  hide the seam's own message from a provider-level arm.
"""

from __future__ import annotations

import importlib
import itertools
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

MODELS = ("FuturesHistorical", "FuturesInfo", "FuturesInstruments", "FuturesCurve", "OptionsChains")
PHASES = ("implementation", "direct", "service", "contract", "counterfact")

#: The scratch evidence root is created by main() so the printed face carries the real path.
SCRATCH = Path(tempfile.mkdtemp(prefix="w29-deribit-preval-"))


def materialize() -> None:
    """Write each fixture the way the producer does: shipped builder, shipped serialization."""
    fixtures = SCRATCH / "fixtures" / "deribit"
    fixtures.mkdir(parents=True, exist_ok=True)
    models = runner.witnesses.PROVIDER_WITNESSES["deribit"]["models"]
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
                    "deribit",
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
            print(f"phase: deribit::{model}::{phase} exit={completed.returncode} {verdict}")
            if completed.returncode != 0:
                print(f"   stderr_tail={completed.stderr.strip().splitlines()[-3:]}")
    total = len(MODELS) * len(PHASES)
    print(f"PHASE_TALLY passed={passed} failed={failed} blocked={blocked} total={total}")
    return failed + blocked


def _entry(model: str) -> dict[str, Any]:
    entry = dict(runner.witnesses.model_entry("deribit", model))
    entry["hosts"] = runner.witnesses.provider_config("deribit")["allowed_hosts"]
    return entry


def _fetch(
    model: str, entry: dict[str, Any], document: dict[str, Any], *, rearm: bool
) -> tuple[list, Any, Any]:
    client = importlib.import_module("opendata.data.providers.deribit.client")
    if rearm:
        client._REQUEST_IDS = itertools.count(1)  # mirrors a fresh phase subprocess
    return runner._run_direct_with_seam("deribit", model, entry, document)


def controls() -> None:
    """Show the faces the widened fake has to refuse, beside the ones that serve."""
    entry = _entry("FuturesHistorical")
    document = entry["fixture_document"]()

    # arm 1: positive -- the POST-shaped, id-echoing fixture serves both chart calls in one process.
    rows, seam, _ = _fetch("FuturesHistorical", entry, document, rearm=True)
    emitted = sorted({type(r).__name__ for r in rows})
    print(
        f"CONTROL_POSITIVE served: rows={len(rows)} classes={emitted} "
        f"methods={seam.methods} urls={len(seam.calls)}"
    )

    # arm 2: two generations without re-arming, which is what the counterfact phase actually does.
    second, _, _ = _fetch("FuturesHistorical", _entry("FuturesHistorical"), document, rearm=False)
    client = importlib.import_module("opendata.data.providers.deribit.client")
    counter_next = next(client._REQUEST_IDS)
    print(f"CONTROL_SECOND_GENERATION served: rows={len(second)} counter_next={counter_next}")

    # arm 3: the null-id placeholder cannot pass silently -- drop the echo declaration.
    no_echo = json.loads(json.dumps(document))
    del no_echo["responses"][0]["echo_request_id"]
    try:
        _fetch("FuturesHistorical", _entry("FuturesHistorical"), no_echo, rearm=True)
    except Exception as exc:  # the refusal is the reading
        print(f"CONTROL_ECHO_DECLARATION_GUARD refused: {type(exc).__name__} {exc}")
    else:
        print("CONTROL_ECHO_DECLARATION_GUARD NOT_FALSIFIED (a null id served)")

    # arm 4: the method key is load-bearing -- a POST route cannot be served by a GET-shaped spec.
    flipped = json.loads(json.dumps(document))
    flipped["responses"][0]["method"] = "GET"
    try:
        _fetch("FuturesHistorical", _entry("FuturesHistorical"), flipped, rearm=True)
    except Exception as exc:
        print(f"CONTROL_VERB_GUARD refused: {type(exc).__name__} {exc}")
    else:
        print("CONTROL_VERB_GUARD NOT_FALSIFIED (method key is decorative)")

    # arm 5: the fixture's page count is a real bound -- a third call to a two-entry sequence fails.
    three = _entry("FuturesHistorical")
    three["query"] = {
        "symbol": ["BTC-PERPETUAL", "ETH-PERPETUAL", "XRP-PERPETUAL"],
        "start_date": "2026-01-01",
        "end_date": "2026-01-01",
        "interval": "1m",
    }
    try:
        runner._run_direct_with_seam("deribit", "FuturesHistorical", three, document)
    except Exception as exc:
        print(f"CONTROL_SEQUENCE_EXHAUSTION refused: {type(exc).__name__} {exc}")
    else:
        print("CONTROL_SEQUENCE_EXHAUSTION NOT_FALSIFIED")

    # 5b: the provider sanitizes, so the seam's own message is read at the seam. Same call shape,
    # two sides: two served pages carrying the ids that were asked, then the third one refusing.
    seam_client = runner._FakeSharedClient([dict(document["responses"][0])])
    chart_url = "https://www.deribit.com/api/v2/public/get_tradingview_chart_data"
    served = [
        json.loads(
            seam_client.request(
                "POST", chart_url, json_body={"jsonrpc": "2.0", "id": asked}
            ).content
        )
        for asked in (1, 2)
    ]
    print(
        f"CONTROL_SEAM_PAGES served_ids={[page['id'] for page in served]} "
        f"distinct_bodies={served[0]['result']['close'] != served[1]['result']['close']}"
    )
    try:
        seam_client.request("POST", chart_url, json_body={"jsonrpc": "2.0", "id": 3})
    except runner.WitnessFailure as exc:
        print(f"CONTROL_SEAM_EXHAUSTION refused: {exc}")
    else:
        print("CONTROL_SEAM_EXHAUSTION NOT_FALSIFIED")


def main() -> int:
    """Block sockets, run the 25 shipped phase verdicts, then print the control faces."""
    runner._block_real_sockets()  # the shipped judge does this first, so does the arm
    materialize()
    tally = run_phases()
    controls()
    print(f"PREVALIDATION_DONE phase_failures_or_blocked={tally}")
    return 0 if tally == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
