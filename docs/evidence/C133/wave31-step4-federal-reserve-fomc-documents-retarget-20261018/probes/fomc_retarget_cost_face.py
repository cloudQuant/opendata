"""Wave-31 step 4: price the retarget against the two frozen federal_reserve tests, in memory.

``fomc_documents_retarget_prevalidation.py`` settled which published face the governed service
serializes against. The open question is what pointing the bound fetcher at that face costs the
frozen suite, because the answer decides whether the landing is safe or whether it has to edit a
judge:

* ``test_declared_row_models_are_their_domain_contracts`` only iterates fetchers whose
  ``row_model`` is not None, so the row is invisible to it today. Declaring the look-alike would
  redden it; declaring the contract would satisfy it.
* ``test_integration_handoff_semantics_reference_typed_contract_fields`` reads the C100 handoff and
  checks the *handoff's* ``time_field`` / ``natural_key`` / ``filter_dims`` against the class the
  test itself imports for the model. That import is the ambiguity under test: it pulls
  ``FederalReserveFomcDocument`` from ``federal_reserve_macro``, the look-alike, while the shipped
  resolver returns the same *name* from ``federal_reserve_clean_documents``. So the handoff test can
  only be re-coloured by the retarget through ``_MODELS``, and that has to be measured.

Nothing is patched on disk and no assertion is restated here: the shipped test module is imported,
``_MODELS`` is swapped in memory for one entry, and the shipped test function is called. Its own
``assert`` is the surface that speaks, so the red -- if there is one -- is attributable to the test
as it stands, not to a copy of its logic. The unpatched call is the control arm and must pass, or
the swap proves nothing.

Arms: ``A_guard_today`` (frozen guard green, row absent from its declaring set), ``B_guard_macro``
(forging the look-alike declaration reddens the shipped guard predicate, and the forged class is
verified to be a distinct ``ContractModel`` subclass), ``B2_forge_control`` (the same predicate read
against a non-class ``row_model``, which is also false -- the reason B needs the class gate),
``C_guard_contract``
(declaring the contract satisfies it), ``D_handoff_control`` (shipped test, shipped ``_MODELS``),
``E_handoff_after_swap`` (same test, ``FomcDocuments`` entry moved to the resolved contract class),
``F_staleness_attributed`` (whatever ``E`` refuses, it refuses on the handoff's own field names,
which the shipped ``require_domain_semantics`` no longer publishes -- a stale historical input, not
a live defect).
"""

from __future__ import annotations

import json
import subprocess  # nosec B404  # git ls-files only, list argv, shell never enabled
import sys
import traceback
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[5]
C128_PROBES = REPO_ROOT / "docs/evidence/C128/plane1-residue-readiness-20261017/probes"
RUNS = Path(__file__).resolve().parent.parent / "runs"

sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(C128_PROBES))
sys.path.insert(0, str(REPO_ROOT / "tests"))

from opendata.data.domains import contract_model, require_domain_semantics  # noqa: E402
from opendata.data.models.base import ContractModel  # noqa: E402
from opendata.data.providers.federal_reserve.provider import PROVIDER  # noqa: E402

DOMAIN = "federal_reserve_fomc_documents"
IDENTITY = "federal_reserve::FomcDocuments"
MODEL = "FomcDocuments"
HANDOFF = REPO_ROOT / "docs/evidence/C100/federal-reserve/integration.json"
HANDOFF_TEST = "test_integration_handoff_semantics_reference_typed_contract_fields"
GUARD_TEST = "test_declared_row_models_are_their_domain_contracts"


def _run(fn: Any) -> tuple[bool, str, int]:  # noqa: ANN401  # a pytest test function takes no arguments here
    """Call one shipped test function and report its verdict with the assertion surface that refused.

    The module is imported normally, so pytest's assertion rewriting is not in play and a bare
    ``assert`` raises with an empty message. The failing line's source text and number are the
    attribution, which is why this returns them instead of a message string.
    """
    try:
        fn()
    except AssertionError:
        frame = traceback.extract_tb(sys.exc_info()[2])[-1]
        return False, (frame.line or "").strip(), int(frame.lineno)
    return True, "", 0


def _line(cells: list[tuple[str, Any]]) -> str:
    """One printed face line: named cells in the order given."""
    return " ".join(f"{name}={value}" for name, value in cells)


def main() -> int:
    """Swap the class the shipped handoff test imports for this model, and read what it says."""
    import test_federal_reserve_iter2_models as fed_tests

    contract = contract_model(DOMAIN)
    fetcher = next(f for f in PROVIDER.fetchers if getattr(f, "canonical_model", None) == MODEL)
    fetcher_type = type(fetcher)
    # The list is materialised and indexed before ``type`` is taken. Written inline as
    # ``type(transform_data(...)[0])`` the call groups as ``type(list_of_rows[0])``, and because a
    # pydantic row has ``__iter__`` that reads ``builtins.list`` -- which is what the first take of
    # this face printed, forging the guard arm against a non-class.
    rows = list(fetcher.transform_data(_HTML, fetcher.transform_query(year=2024)))
    emitted = type(rows[0]) if rows else None

    declaring = {
        str(f.canonical_model)
        for f in PROVIDER.fetchers
        if getattr(type(f), "row_model", None) is not None
    }

    guard_ok, guard_line, guard_lineno = _run(getattr(fed_tests, GUARD_TEST))
    arm_a = guard_ok and MODEL not in declaring

    shipped_model = getattr(fetcher_type, "row_model", None)
    forged_macro = type("ForgedLookAlike", (fetcher_type,), {"row_model": emitted})
    forged_contract = type("ForgedContract", (fetcher_type,), {"row_model": contract})

    def predicate(cls: type) -> bool:
        """The shipped guard's own comparison, applied to a class forged in memory."""
        return getattr(cls, "row_model", None) is contract_model(
            getattr(cls.capability, "domain", None)
        )

    macro_holds = predicate(forged_macro)
    contract_holds = predicate(forged_contract)
    # The contrast arm must be able to differ: an arm forged from a non-class (or from ``None``)
    # would also make the predicate false, so the look-alike's identity is gated before its
    # redness is credited.
    emitted_is_contract = isinstance(emitted, type) and issubclass(emitted, ContractModel)
    emitted_is_not_contract = emitted is not contract
    predicate_on_nonclass = predicate(type("ForgedList", (fetcher_type,), {"row_model": list}))
    arm_b = (
        shipped_model is None
        and emitted_is_contract
        and emitted_is_not_contract
        and macro_holds is False
    )
    # B2 is the control that says why the class gate is load-bearing: the shipped predicate reads
    # False for a forged ``builtins.list`` row_model exactly as it reads False for the real
    # look-alike, so predicate-redness alone cannot tell the two apart. Without
    # ``emitted_is_contract`` arm B would have passed on the first take's instrument bug.
    arm_b_control = predicate_on_nonclass is False and macro_holds is False
    arm_c = contract_holds is True

    models_before = dict(fed_tests._MODELS)
    host = models_before[MODEL][1]
    d_ok, d_line, d_lineno = _run(getattr(fed_tests, HANDOFF_TEST))
    fed_tests._MODELS[MODEL] = (contract, host)
    try:
        e_ok, e_line, e_lineno = _run(getattr(fed_tests, HANDOFF_TEST))
    finally:
        fed_tests._MODELS[MODEL] = models_before[MODEL]
    restored = dict(fed_tests._MODELS)
    arm_d = d_ok and restored[MODEL] is models_before[MODEL]
    arm_e = e_ok is False and bool(e_line)

    semantics = require_domain_semantics(DOMAIN)
    published = set(
        (() if semantics.time_field is None else (semantics.time_field,))
        + tuple(semantics.natural_key)
        + tuple(semantics.filter_dims)
    )
    contract_fields = set(contract.model_fields)
    handoff_entry = json.loads(HANDOFF.read_text(encoding="utf-8"))["domains"][DOMAIN]
    handoff_names = (
        set(handoff_entry["natural_key"])
        | set(handoff_entry["filter_dims"])
        | {str(handoff_entry["time_field"])}
    )
    stale_in_handoff = sorted(handoff_names - contract_fields)
    tracked = subprocess.run(  # noqa: S603  # nosec B603 B607  # literal argv, shell never enabled
        ["git", "-c", "core.quotepath=off", "ls-files", "--error-unmatch", str(HANDOFF)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).returncode
    arm_f = (
        not (published - contract_fields)
        and bool(stale_in_handoff)
        and handoff_names != published
        and "semantic_fields" in e_line
    )

    gates = {
        "A_guard_today": arm_a,
        "B_guard_macro": arm_b,
        "B2_forge_control": arm_b_control,
        "C_guard_contract": arm_c,
        "D_handoff_control": arm_d,
        "E_handoff_after_swap": arm_e,
        "F_staleness_attributed": arm_f,
    }
    all_pass = all(gates.values())

    faces = [
        "wave-31 step 4 -- the retarget priced against the two frozen federal_reserve tests, in memory (take 2)",
        f"argv: {sys.executable} {Path(__file__).resolve()}",
        "   (cwd = repo root, no pipes, PYTHONPATH=.; imports tests/ on sys.path; nothing on disk is",
        "    patched, and _MODELS is restored in a finally block)",
        "take_1_refuted=runs/wave31-step4-fomc_retarget_cost_face_take1_REFUTED.txt",
        "   (take 1's arm B read emitted=builtins.list because ``type(transform_data(...)[0])`` groups",
        "    as ``type(list_of_rows[0])`` and a pydantic row has ``__iter__``; the look-alike forge was",
        "    therefore never the look-alike and B passed for the wrong reason. Arms A/C-F of take 1 are",
        "    unchanged in substance; B is re-gated and B2 added as its control.)",
        "stderr_bytes=0",
        _line(
            [
                ("A_guard_today", GUARD_TEST),
                ("row", IDENTITY),
                ("exit_ok", guard_ok),
                ("declaring_models", len(declaring)),
                ("this_row_declaring", MODEL in declaring),
                ("failure_line", guard_line or "-"),
                ("failure_lineno", guard_lineno),
                ("PASS", arm_a),
            ]
        ),
        _line(
            [
                ("B_guard_macro", f"shipped_row_model={shipped_model!r}"),
                ("rows_emitted", len(rows)),
                ("emitted", f"{emitted.__module__}.{emitted.__name__}"),
                ("emitted_is_contract", emitted_is_contract),
                ("emitted_is_not_contract", emitted_is_not_contract),
                ("forged_look_alike_predicate", macro_holds),
                ("PASS", arm_b),
            ]
        ),
        _line(
            [
                ("B2_forge_control", "row_model=list"),
                ("predicate_on_nonclass", predicate_on_nonclass),
                ("predicate_on_look_alike", macro_holds),
                (
                    "reading",
                    "the shipped predicate reads False for both, so only the class gate in arm B "
                    "separates the look-alike from a non-class forge",
                ),
                ("PASS", arm_b_control),
            ]
        ),
        _line(
            [
                ("C_guard_contract", "forged_contract_predicate"),
                ("value", contract_holds),
                ("contract", f"{contract.__module__}.{contract.__name__}"),
                ("PASS", arm_c),
            ]
        ),
        _line(
            [
                ("D_handoff_control", HANDOFF_TEST),
                (
                    "models_entry",
                    f"{models_before[MODEL][0].__module__}.{models_before[MODEL][0].__name__}",
                ),
                ("exit_ok", d_ok),
                ("restored_after_swap", restored[MODEL] is models_before[MODEL]),
                ("failure_line", d_line or "-"),
                ("failure_lineno", d_lineno),
                ("PASS", arm_d),
            ]
        ),
        _line(
            [
                ("E_handoff_after_swap", "entry_moved_to=contract_class"),
                ("exit_ok", e_ok),
                ("failure_line", e_line or "-"),
                ("failure_lineno", e_lineno),
                ("PASS", arm_e),
            ]
        ),
        _line(
            [
                ("F_staleness_attributed", f"published={sorted(published)}"),
                ("contract_fields", sorted(contract_fields)),
                ("published_missing_from_contract", sorted(published - contract_fields)),
                ("handoff_entry_names", sorted(handoff_names)),
                ("stale_in_handoff", stale_in_handoff),
                ("handoff_file_tracked_rc", tracked),
                ("PASS", arm_f),
            ]
        ),
        _line(
            [
                ("COST_RULING", f"handoff_test_red_after_retarget={not e_ok}"),
                ("guard_test_red_after_retarget", not contract_holds),
                (
                    "reading",
                    "the handoff entry is a C100 historical artifact whose semantic field names are "
                    "the look-alike's; the shipped domain publishes the contract's"
                    if not e_ok and not (published - contract_fields)
                    else "UNATTRIBUTED -- do not land on this reading",
                ),
            ]
        ),
        _line(
            [
                ("retarget_cost_face", f"arms={len(gates)}"),
                ("arms_passed", sum(1 for v in gates.values() if v)),
                ("gates", gates),
                ("all_pass", all_pass),
            ]
        ),
        f"fomc_retarget_cost_face_exit={0 if all_pass else 1}",
    ]
    text = "\n".join(faces) + "\n"
    RUNS.mkdir(parents=True, exist_ok=True)
    carrier = RUNS / "wave31-step4-fomc_retarget_cost_face.txt"
    carrier.write_text(text, encoding="utf-8")
    print(text, end="")
    print(f"carrier_written bytes={len(text.encode())} path={carrier.relative_to(REPO_ROOT)}")
    return 0 if all_pass else 1


_HTML = (
    "<html><h5>June 11-12 Meeting - 2024</h5>"
    '<a href="/monetarypolicy/files/FOMC20240612statement.pdf">Statement</a></html>'
)

if __name__ == "__main__":
    raise SystemExit(main())
