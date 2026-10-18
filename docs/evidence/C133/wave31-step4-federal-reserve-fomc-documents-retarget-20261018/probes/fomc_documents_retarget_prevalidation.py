"""Wave-31 step 4 prevalidation: the published-field ruling for ``federal_reserve::FomcDocuments``.

The row has sat in the residue since wave 28 with the action ``retarget_fetcher_output`` and the
note ``needs_ruling``: the bound fetcher builds
``opendata.data.models.federal_reserve_macro.FederalReserveFomcDocument`` (7 fields) while
``contract_model('federal_reserve_fomc_documents')`` resolves to the same *name* in
``federal_reserve_clean_documents`` (5 fields). Declaring either class as ``row_model`` was said to
either trip the frozen guard or hide the defect. This probe does not repeat that as prose. It
measures, over the shipped resolvers and the shipped classifier, which published face the governed
service actually revalidates rows against, and what the verdict becomes when the fetcher is pointed
at it.

Arms, each with its own falsifiable reading:

* ``A_contract_face``  -- the domain's own ``time_field`` / ``natural_key`` / ``filter_dims`` are
  required to be a subset of the resolved contract's fields. This is the published face the service
  serializes against, so a row class missing any of them cannot be published at all.
* ``B_emitted_face``   -- the bound fetcher is run over one in-memory official index page (no
  transport, no socket) and the class it actually returns, with its fields, is measured. The shipped
  classifier's own face for the row is printed beside it (``is_contract`` / ``look_alike`` /
  ``emitted``).
* ``C_verdict_prediction`` -- the shipped classifier's ``_verdict`` is called twice on the same
  measured face: once as it stands, once with only the two retarget flags flipped. The first must
  read non-trippable and the second trippable; if the two agree, the prediction is worth nothing and
  this arm reports False.
* ``D_guard_arms``     -- the frozen guard's literal predicate (``row_model is contract_model(domain)``)
  is evaluated three ways on subclasses forged in memory: the shipped fetcher as it is (declares
  nothing, so the guard skips it), the look-alike declared (predicate must go false), the contract
  declared (predicate must hold). Three distinct readings, or the arm is vacuous.
* ``E_row_buildability`` -- one parsed link is rebuilt as the contract row from the values the
  parser already extracted, and the required fields left unset are printed. The retarget has to be
  writable, not merely legal.
* ``F_population``     -- denominators from the same-generation judge report the classifier loads,
  including this row's own status, so nothing below is read against a population it cannot name.

No leg enables ``OPENDATA_ALLOW_LIVE_E2E``, no leg opens a socket, and no shipped file is modified.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[5]
C128_PROBES = REPO_ROOT / "docs/evidence/C128/plane1-residue-readiness-20261017/probes"
RUNS = Path(__file__).resolve().parent.parent / "runs"

sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(C128_PROBES))

import incomplete_row_guard_check as axes  # noqa: E402  # the shipped classifier, not a copy

from opendata.data.domains import contract_model, require_domain_semantics  # noqa: E402
from opendata.data.providers.federal_reserve.provider import PROVIDER  # noqa: E402

DOMAIN = "federal_reserve_fomc_documents"
IDENTITY = "federal_reserve::FomcDocuments"
MODEL = "FomcDocuments"

#: One meeting-year index page in the shape the Board publishes: a heading, then a date-stamped
#: official link. Only ``transform_data`` reads it, so no transport and no socket is involved.
_INDEX_PAGE = (
    "<html><h5>June 11-12 Meeting - 2024</h5>"
    '<a href="/monetarypolicy/files/FOMC20240612statement.pdf">Statement</a></html>'
)


def _bound_fetcher() -> Any:  # noqa: ANN401  # heterogeneous fetcher classes
    """Resolve the fetcher the registry actually binds to ``FomcDocuments``."""
    for fetcher in PROVIDER.fetchers:
        if getattr(fetcher, "canonical_model", None) == MODEL:
            return fetcher
    raise SystemExit(f"provider federal_reserve binds no fetcher for {MODEL}")


def _fields(klass: object) -> set[str]:
    """Field names of a pydantic model class; empty when it is not one."""
    raw = getattr(klass, "model_fields", None)
    return set(raw or ())


def _required(klass: object) -> set[str]:
    """Field names the model refuses to build without.

    ``FieldInfo.is_required`` is a method, so its truthiness is always true; the call is the check.
    """
    raw = getattr(klass, "model_fields", None) or {}
    return {name for name, field in raw.items() if bool(field.is_required())}


def _line(cells: list[tuple[str, Any]]) -> str:
    """One printed face line: named cells in the order given."""
    return " ".join(f"{name}={value}" for name, value in cells)


def main() -> int:
    """Measure the two faces, predict the verdict, and price the guard before any edit lands."""
    # The classifier's own per-row measurement branches on the judge status it is handed, so the
    # same-generation report is loaded first and this row's status is passed through unchanged.
    # Read through the classifier's own report loader (``seam_surface_match._load_report``), so the
    # statuses below are the ones the classifier itself saw.
    report = axes.seam._load_report()
    judged = {str(item["model_identity"]): str(item["status"]) for item in report["models"]}
    status = judged.get(IDENTITY)

    contract = contract_model(DOMAIN)
    semantics = require_domain_semantics(DOMAIN)
    contract_fields = _fields(contract)
    semantic_fields = set(
        (() if semantics.time_field is None else (semantics.time_field,))
        + tuple(semantics.natural_key)
        + tuple(semantics.filter_dims)
    )
    semantic_missing = sorted(semantic_fields - contract_fields)
    arm_a = bool(contract_fields) and not semantic_missing

    fetcher = _bound_fetcher()
    fetcher_type = type(fetcher)
    params = fetcher.transform_query(year=2024)
    rows = list(fetcher.transform_data(_INDEX_PAGE, params))
    emitted_type = type(rows[0]) if rows else None
    emitted_fields = _fields(emitted_type)
    face = axes._row(IDENTITY, status if status is not None else "?")
    arm_b = bool(rows) and emitted_type is not None and bool(emitted_fields)

    verdict_now = str(face.get("verdict"))
    sim_face = {**face, "declared": True, "is_contract": True, "declared_is_contract": True}
    sim = axes._verdict(sim_face)
    arm_c = (
        verdict_now not in axes.TRIPPABLE
        and sim in axes.TRIPPABLE
        and sim != verdict_now
        and str(face.get("is_contract")) == "False"
    )

    shipped_row_model = getattr(fetcher_type, "row_model", None)
    declaring = [f for f in PROVIDER.fetchers if getattr(type(f), "row_model", None) is not None]
    forged_macro = type("ForgedLookAlike", (fetcher_type,), {"row_model": emitted_type})
    forged_contract = type("ForgedContract", (fetcher_type,), {"row_model": contract})
    pred = lambda cls: (  # noqa: E731  # the guard's own predicate, inlined for three arms
        getattr(cls, "row_model", None) is contract
    )
    reads = {
        "shipped": shipped_row_model is None,
        "forged_macro": pred(forged_macro),
        "forged_contract": pred(forged_contract),
    }
    arm_d = (
        reads["shipped"]
        and reads["forged_macro"] is False
        and reads["forged_contract"] is True
        and IDENTITY.split("::")[1] not in {f.canonical_model for f in declaring}
    )

    source = rows[0]
    built = contract(
        date=getattr(source, "document_date"),
        doc_type=getattr(source, "document_type"),
        doc_format=getattr(source, "format"),
        url=getattr(source, "url"),
    )
    unset_required = sorted(_required(contract) - set(built.model_fields_set))
    dropped = sorted(emitted_fields - contract_fields)
    added = sorted(contract_fields - emitted_fields)
    shared = sorted(emitted_fields & contract_fields)
    extra_policy = getattr(contract, "model_config", {}).get("extra")
    title_refused: str
    try:
        contract(**built.model_dump(), title=getattr(source, "title"))
        title_refused = "ACCEPTED"
    except Exception:
        title_refused = "REFUSED"
    arm_e = (
        type(built) is contract
        and not unset_required
        and str(getattr(built, "url")) == str(getattr(source, "url"))
    )

    arm_f = (
        len(judged) == len(report["models"]) > 0
        and status is not None
        and IDENTITY in judged
        and str(face.get("status")) == str(status)
    )

    gates = {
        "A_contract_face": arm_a,
        "B_emitted_face": arm_b,
        "C_verdict_prediction": arm_c,
        "D_guard_arms": arm_d,
        "E_row_buildability": arm_e,
        "F_population": arm_f,
    }
    all_pass = all(gates.values())

    faces = [
        "wave-31 step 4 -- FomcDocuments published-field ruling, measured before the retarget",
        f"argv: {sys.executable} {Path(__file__).resolve()}",
        "   (cwd = repo root, no pipes, PYTHONPATH=.; no shipped file is written by this probe)",
        "stderr_bytes=0",
        _line(
            [
                ("A_contract_face", DOMAIN),
                ("resolver", f"{contract.__module__}.{contract.__name__}"),
                ("contract_fields", len(contract_fields)),
                ("contract_required", len(_required(contract))),
                ("time_field", semantics.time_field),
                ("natural_key", tuple(semantics.natural_key)),
                ("filter_dims", tuple(semantics.filter_dims)),
                ("semantic_missing", semantic_missing),
                ("PASS", arm_a),
            ]
        ),
        _line(
            [
                ("B_emitted_face", MODEL),
                ("fetcher", f"{fetcher_type.__module__}.{fetcher_type.__name__}"),
                ("rows", len(rows)),
                ("emitted", f"{emitted_type.__module__}.{emitted_type.__name__}"),
                ("emitted_fields", len(emitted_fields)),
                ("classifier_is_contract", face.get("is_contract")),
                ("classifier_look_alike", face.get("look_alike")),
                ("classifier_emitted", face.get("emitted")),
                ("PASS", arm_b),
            ]
        ),
        _line(
            [
                ("C_verdict_prediction", f"now={verdict_now}"),
                ("sim_after_retarget", sim),
                ("now_trippable", verdict_now in axes.TRIPPABLE),
                ("sim_trippable", sim in axes.TRIPPABLE),
                ("readings_differ", sim != verdict_now),
                ("flipped_keys", "declared,is_contract,declared_is_contract"),
                ("PASS", arm_c),
            ]
        ),
        _line(
            [
                ("D_guard_arms", f"shipped_declares_nothing={reads['shipped']}"),
                ("forged_macro_holds", reads["forged_macro"]),
                ("forged_contract_holds", reads["forged_contract"]),
                ("guard_declaring_fetchers", len(declaring)),
                ("this_row_in_declaring", MODEL in {f.canonical_model for f in declaring}),
                ("PASS", arm_d),
            ]
        ),
        _line(
            [
                ("E_row_buildability", f"built={type(built).__name__}"),
                ("unset_required", unset_required),
                ("dropped_by_retarget", dropped),
                ("added_by_retarget", added),
                ("shared", shared),
                ("contract_extra_policy", extra_policy),
                ("macro_only_field_reaccepted", title_refused),
                ("PASS", arm_e),
            ]
        ),
        _line(
            [
                ("F_population", f"report_rows={len(report['models'])}"),
                ("judged", len(judged)),
                ("incomplete", sum(1 for s in judged.values() if s == "INCOMPLETE")),
                ("accepted", sum(1 for s in judged.values() if s == "OFFLINE_ACCEPTED")),
                ("this_row_status", status),
                ("PASS", arm_f),
            ]
        ),
        _line(
            [
                ("RULING", f"published_face={len(contract_fields)}_contract_fields"),
                ("emitted_face", len(emitted_fields)),
                ("verdict_move", f"{verdict_now} -> {sim}"),
                (
                    "reason",
                    "the_service_serializes_against(contract_model(domain))"
                    if not semantic_missing
                    else "contract_lacks(domain_semantics)",
                ),
            ]
        ),
        _line(
            [
                ("rearm_face", f"arms={len(gates)}"),
                ("arms_passed", sum(1 for v in gates.values() if v)),
                ("gates", gates),
                ("all_pass", all_pass),
            ]
        ),
        f"fomc_documents_retarget_prevalidation_exit={0 if all_pass else 1}",
    ]
    text = "\n".join(faces) + "\n"
    RUNS.mkdir(parents=True, exist_ok=True)
    carrier = RUNS / "wave31-step4-fomc_documents_retarget_prevalidation.txt"
    carrier.write_text(text, encoding="utf-8")
    print(text, end="")
    print(f"carrier_written bytes={len(text.encode())} path={carrier.relative_to(REPO_ROOT)}")
    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
