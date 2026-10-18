r"""Diagnose why all five deribit counterfacts refused at the envelope, not at the tamper.

This is the instrument behind the wave-29 decision to widen the seam instead of re-declaring the
codes. Hypothesis: ``witness_counterfact`` runs two fetch generations in one process (control run,
then tampered run) while ``client._REQUEST_IDS`` is a process-global ``itertools.count(1)``, so the
*second* generation asks an id a fixed-id fixture cannot echo, and ``_parse_reply`` refuses with
``DERIBIT_BAD_ENVELOPE`` before the tamper reaches a parser. Each model is driven twice with the
counter re-armed between runs -- exactly what a fresh subprocess would give -- to show whether the
declared tamper itself bites with the declared code.

Two readings matter and they are different: ``DERIBIT_BAD_ENVELOPE`` for every model in the real
``counterfact`` phase is the harness's fault, while ``declared_code_matched=5`` under the re-armed
counter is the fixture's exoneration. The second is what rules out "the tampers are wrong" and makes
the echo declaration, rather than a copied refusal code, the fix.
"""

from __future__ import annotations

import importlib
import itertools
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(REPO_ROOT / "scripts/quality"))
sys.path.insert(0, str(REPO_ROOT))

runner = importlib.import_module("iter2_claims_probe_runner")
deribit_client = importlib.import_module("opendata.data.providers.deribit.client")

MODELS = ("FuturesHistorical", "FuturesInfo", "FuturesInstruments", "FuturesCurve", "OptionsChains")


def _entry(model: str) -> dict[str, Any]:
    entry = dict(runner.witnesses.model_entry("deribit", model))
    entry["hosts"] = runner.witnesses.provider_config("deribit")["allowed_hosts"]
    return entry


def _run(model: str, entry: dict[str, Any], document: dict[str, Any]) -> tuple[list[str], int]:
    deribit_client._REQUEST_IDS = itertools.count(1)  # this arm IS the hypothesis
    rows, seam, _ = runner._run_direct_with_seam("deribit", model, entry, document)
    return list(seam.methods), len(rows)


def main() -> int:
    """Drive control and tampered runs per model, re-arming the request-id counter between them."""
    runner._block_real_sockets()
    matches = 0
    for model in MODELS:
        entry = _entry(model)
        document = entry["fixture_document"]()
        tampered = runner._apply_tamper(document, entry["tamper"])
        tamper_op = entry["tamper"]["op"]
        tamper_path = entry["tamper"]["path"]
        control_verbs, control_rows = _run(model, entry, document)
        try:
            _run(model, entry, tampered)
        except Exception as exc:  # the observed refusal is the reading
            observed_code = getattr(exc, "code", None)
            note = f"{type(exc).__name__} code={observed_code}"
        else:
            observed_code = None
            note = "NO_REFUSAL (tamper did not bite)"
        declared = entry["refusal_code"]
        ok = observed_code == declared
        matches += int(ok)
        print(
            f"rearm_counter model={model} control_verbs={control_verbs} "
            f"control_rows={control_rows} "
            f"tamper_op={tamper_op} tamper_path={json.dumps(tamper_path)} "
            f"declared={declared} observed={note} match={ok}"
        )
    print(f"REARM_TALLY models={len(MODELS)} declared_code_matched={matches}")
    return 0 if matches == len(MODELS) else 1


if __name__ == "__main__":
    sys.exit(main())
