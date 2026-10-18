"""Wave-31 step 4: price the re-based handoff judge, so "green" is not the same word as "blind".

``tests/test_federal_reserve_iter2_models.py::test_integration_handoff_semantics_reference_typed_contract_fields``
was re-priced against the shipped registry in this step, because the face it compared -- the C100
handoff's own semantic spellings against whichever class its ``contract`` name resolved to in the
test's table -- was only satisfiable while the table imported the look-alike
``federal_reserve_macro.FederalReserveFomcDocument``. Editing a judge is a claim
about the judge, not about the row, so every arm below calls the SHIPPED test function and lets its
own ``assert`` speak. Nothing on disk is patched; ``_MODELS`` and the test module's
``require_domain_semantics`` binding are swapped in memory and restored in a ``finally``.

Arms: ``A_shipped_green`` (the edited judge as it stands), ``B_look_alike_reddens`` (the identity
assertion, forged back to the macro class), ``C_semantic_drift_reddens`` (a shipped domain
publishing a ``time_field`` its contract does not have), ``D_population_pinned`` (deleting the table
entry reddens the domain-population assertion, so the loop cannot be vacuous),
``E_retired_comparison_recorded``
(what the retired check said: the handoff's three stale names are on the look-alike and not on the
contract -- the ``#68`` ruling, kept in the face rather than deleted with the check).

The module is imported normally, so pytest's assertion rewriting is not in play and a bare
``assert`` raises with an empty message; the failing line's text and number are the attribution.
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[5]
RUNS = Path(__file__).resolve().parent.parent / "runs"
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests"))

from opendata.data.domains import DomainSpec, contract_model  # noqa: E402
from opendata.data.models.federal_reserve_macro import (  # noqa: E402
    FederalReserveFomcDocument as MacroLookAlike,
)

JUDGE = "test_integration_handoff_semantics_reference_typed_contract_fields"
DOMAIN = "federal_reserve_fomc_documents"
MODEL = "FomcDocuments"
HANDOFF = REPO_ROOT / "docs/evidence/C100/federal-reserve/integration.json"


def _run(fn: Any) -> tuple[bool, str, int]:  # noqa: ANN401  # the shipped test takes no arguments
    """Call one shipped test function; report its verdict and the assertion surface that refused."""
    try:
        fn()
    except AssertionError:
        frame = traceback.extract_tb(sys.exc_info()[2])[-1]
        return False, (frame.line or "").strip(), int(frame.lineno)
    return True, "", 0


def _line(cells: list[tuple[str, Any]]) -> str:
    """One printed face line: named cells in the order given."""
    return " ".join(f"{name}={value}" for name, value in cells)


def _tampered_semantics(tamper_domain: str, looped: list[str]) -> Any:  # noqa: ANN401  # a require_domain_semantics stand-in
    """Record the population ``require_domain_semantics`` is asked for, and break one domain."""
    from opendata.data.domains import require_domain_semantics as shipped

    def fake(domain: str) -> DomainSpec:
        looped.append(domain)
        spec = shipped(domain)
        if domain != tamper_domain:
            return spec
        return DomainSpec(**{**spec.model_dump(), "time_field": "not_a_contract_field"})

    return fake


def main() -> int:
    """Forge each failure the edited judge is supposed to have, and read what it says."""
    import test_federal_reserve_iter2_models as fed_tests

    contract = contract_model(DOMAIN)
    shipped_semantics = fed_tests.require_domain_semantics
    models_before = dict(fed_tests._MODELS)
    host = models_before[MODEL][1]
    faces: list[str] = []

    a_ok, a_line, a_no = _run(getattr(fed_tests, JUDGE))

    control_looped: list[str] = []
    fed_tests.require_domain_semantics = _tampered_semantics("no_such_domain", control_looped)
    counted_ok, _c_line, _c_no = _run(getattr(fed_tests, JUDGE))
    drift_looped: list[str] = []
    fed_tests.require_domain_semantics = _tampered_semantics(DOMAIN, drift_looped)
    c_ok, c_line, c_no = _run(getattr(fed_tests, JUDGE))
    fed_tests.require_domain_semantics = shipped_semantics

    fed_tests._MODELS[MODEL] = (MacroLookAlike, host)
    try:
        b_ok, b_line, b_no = _run(getattr(fed_tests, JUDGE))
    finally:
        fed_tests._MODELS[MODEL] = models_before[MODEL]

    del fed_tests._MODELS[MODEL]
    try:
        d_ok, d_line, d_no = _run(getattr(fed_tests, JUDGE))
    finally:
        fed_tests._MODELS[MODEL] = models_before[MODEL]

    restored = dict(fed_tests._MODELS)
    e_ok_after = _run(getattr(fed_tests, JUDGE))

    handoff = json.loads(HANDOFF.read_text(encoding="utf-8"))
    spec = DomainSpec.model_validate(handoff["domains"][DOMAIN])
    handoff_names = set(
        (() if spec.time_field is None else (spec.time_field,))
        + spec.natural_key
        + spec.filter_dims
    )
    stale_vs_contract = sorted(handoff_names - set(contract.model_fields))
    stale_vs_look_alike = sorted(handoff_names - set(MacroLookAlike.model_fields))

    gates = {
        "A_shipped_green": a_ok,
        "B_look_alike_reddens": (not b_ok) and bool(b_line),
        "C_semantic_drift_reddens": (not c_ok)
        and bool(c_line)
        and counted_ok
        and len(control_looped) == len(drift_looped) == len(handoff["domains"]),
        "D_population_pinned": (not d_ok) and bool(d_line),
        "E_retired_comparison_recorded": bool(stale_vs_contract) and not stale_vs_look_alike,
        "F_restored_green": restored[MODEL] is models_before[MODEL] and e_ok_after[0],
    }
    all_pass = all(gates.values())

    faces += [
        "wave-31 step 4 -- the re-based handoff judge, priced by forging each failure it must have",
        f"argv: {sys.executable} {Path(__file__).resolve()}",
        "   (cwd = repo root, no pipes, PYTHONPATH=.; the shipped test function is called, never",
        "    copied; _MODELS and require_domain_semantics are restored in a finally block)",
        "stderr_bytes=0",
        _line(
            [
                ("A_shipped_green", JUDGE),
                ("exit_ok", a_ok),
                ("failure_line", a_line or "-"),
                ("failure_lineno", a_no),
                ("PASS", gates["A_shipped_green"]),
            ]
        ),
        _line(
            [
                ("B_look_alike_reddens", "row_model_forged_back_to=federal_reserve_macro"),
                ("exit_ok", b_ok),
                ("failure_line", b_line or "-"),
                ("failure_lineno", b_no),
                ("PASS", gates["B_look_alike_reddens"]),
            ]
        ),
        _line(
            [
                ("C_semantic_drift_reddens", f"tampered_time_field_on={DOMAIN}"),
                ("control_untampered_exit_ok", counted_ok),
                ("control_domains_looped", len(control_looped)),
                ("drift_domains_looped", len(drift_looped)),
                ("domains_in_handoff", len(handoff["domains"])),
                ("table_entries", len(models_before)),
                ("exit_ok", c_ok),
                ("failure_line", c_line or "-"),
                ("failure_lineno", c_no),
                ("PASS", gates["C_semantic_drift_reddens"]),
            ]
        ),
        _line(
            [
                ("D_population_pinned", "models_table_entry_deleted=FomcDocuments"),
                ("exit_ok", d_ok),
                ("failure_line", d_line or "-"),
                ("failure_lineno", d_no),
                ("PASS", gates["D_population_pinned"]),
            ]
        ),
        _line(
            [
                ("E_retired_comparison_recorded", f"handoff_names={sorted(handoff_names)}"),
                ("missing_on_shipped_contract", stale_vs_contract),
                ("missing_on_look_alike", stale_vs_look_alike),
                (
                    "reading",
                    "the retired check was green only while the table imported the "
                    "look-alike, so a green there measured the defect and not the contract",
                ),
                ("PASS", gates["E_retired_comparison_recorded"]),
            ]
        ),
        _line(
            [
                ("F_restored_green", "swap_undone=True"),
                ("exit_ok", e_ok_after[0]),
                ("failure_line", e_ok_after[1] or "-"),
                ("failure_lineno", e_ok_after[2]),
                ("PASS", gates["F_restored_green"]),
            ]
        ),
        _line(
            [
                (
                    "JUDGE_RULING",
                    "live_faces_priced=imported_class_is_resolved_contract"
                    "+shipped_semantics_are_contract_fields",
                ),
                ("retired_face", "handoff_spellings_vs_resolved_class"),
                ("all_arms_pass", all_pass),
            ]
        ),
        _line(
            [
                ("handoff_judge_counterfact", f"arms={len(gates)}"),
                ("arms_passed", sum(1 for v in gates.values() if v)),
                ("gates", gates),
                ("all_pass", all_pass),
            ]
        ),
        f"handoff_judge_counterfact_exit={0 if all_pass else 1}",
    ]
    text = "\n".join(faces) + "\n"
    RUNS.mkdir(parents=True, exist_ok=True)
    carrier = RUNS / "wave31-step4-handoff_judge_counterfact.txt"
    carrier.write_text(text, encoding="utf-8")
    print(text, end="")
    print(f"carrier_written bytes={len(text.encode())} path={carrier.relative_to(REPO_ROOT)}")
    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
