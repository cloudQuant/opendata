"""Gate the claim that yfinance's declared rows cannot be witnessed through either harness seam.

Wave 27 step 1 declared ``row_model`` on the 15 convertible yfinance rows, so the pricer's
``NEED_DECLARE`` population fell from 52 to 37 and the package's ``DECLARE_AND_WITNESS`` bucket
from 26 to 11. A declaration is not a witness: plane 1 moves only when five phases run through an
offline seam the harness can actually install. This probe measures which seams the harness offers
and which of them a yfinance fetch leg reads, because the wave's precondition said "ride the
engine ``transport`` leg" on the strength of the seam census label ``engine_transport`` -- and that
label names the *fallback bucket*, not a proven reachability.

Arms, all of which can fail:

``A_engine_transport_control``
    landed witness entries that declare no ``client_seam`` are run through the shipped
    :func:`iter2_claims_probe_runner._run_direct` recipe, with the entry assembled the way the
    runner's ``main`` assembles it (``hosts`` from the provider config and the config's
    ``credential_env`` fixture tokens granted into the process, neither of which a hand-made entry
    has by default). Up to ``--controls`` candidates are attempted in provider order and every
    attempt is printed with its rows/transport-call/error face, so a control that fails is visible
    rather than silently dropped. Each arm-A pass must return rows *and* a non-empty
    ``transport.calls``; without that pass a zero in arm B is indistinguishable from a broken
    experiment.
``B_yfinance_leg_never_reaches_the_seam``
    ``yfinance::EquityQuote`` goes through the same lines with an injected
    ``FixedResponseTransport`` and a placeholder document (the row has no witness module, so no
    fixture file exists, and the claim is only about whether the leg *calls* the transport, so the
    bytes are never consumed). The fetch must raise and must leave ``transport.calls == 0``, and the
    refusal must not be a ``TypeError`` -- a query-shape complaint would mean this experiment, not
    the package, is what failed. The package's leg is ``models/_sdk.py`` ->
    ``governed_yfinance_session`` -> curl_cffi, which never reads ``http_transport``, so the socket
    block in the runner's process is what answers it.
``C_static_seam_attribute_census``
    per provider in the residue, count reads of ``http_transport`` and of
    ``get_shared_http_client`` across the package's own ``.py`` files. yfinance must read 0 of both
    while at least one provider reads a non-zero count of each, so arm B's zero is a property of
    this package and not of the counter.
``D_harness_installs_a_zero_arg_factory``
    the runner's only attribute-patching seam replaces ``attr`` with a lambda that takes no
    argument and returns the fake client, so it can serve ``module.attr()`` but not
    ``module.require_history_snapshot(symbol, ...)``. The package's own tests patch exactly those
    callables, which is the shape the harness lacks. Both halves are read off the AST.
``F_sdk_call_surface``
    the arity the harness would have to serve, read off the package rather than asserted: every
    ``_sdk.<name>(...)`` call site in ``yfinance`` and the smallest positional-argument count seen
    for that name. It must be non-empty with a minimum of at least 1, because that is the gap arm D
    describes -- a witness seam for this package has to hand a *callable with arguments*, not a
    no-arg client factory.
``E_declaration_face``
    every non-accepted ``yfinance`` residue row is run through the shipped per-row judge
    (:func:`incomplete_row_guard_check._row`), and the rows it reads ``declared`` are listed -- with
    no hand-carried identity list, since no residue row declared before this round. The count must
    equal ``DECLARED_EXPECTED`` and each ``row_model`` must be *identical* to
    ``contract_model(capability.domain)``, which is the same identity rule
    ``tests/test_federal_reserve_iter2_models.py:382-402`` enforces for federal_reserve.

Exits non-zero when an arm fails. Arms B and E are the two findings -- the package cannot be
witnessed through either shipped seam, and the 15 declarations this round landed are the product
step that made that worth measuring -- and arms A, C, D and the report face are what make each read
a measurement rather than an assertion.

    PY=/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python
    $PY docs/evidence/C128/plane1-residue-readiness-20261017/probes/witness_seam_kind_census.py

Run straight, never through a pipe. The probe opens no socket: :func:`_block_sockets` is the
runner's own refusal, installed before any fetch is attempted.
"""

from __future__ import annotations

import argparse
import ast
import inspect
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import declare_candidates as dc  # the shipped bucket rule and population
import incomplete_row_guard_check as guard  # the shipped per-row declaration face
import seam_surface_match as seam  # the shipped judge-report reader

REPO_ROOT = seam.REPO_ROOT
QUALITY = REPO_ROOT / "scripts" / "quality"
sys.path.insert(0, str(QUALITY))

import iter2_claims_fixtures as witnesses  # noqa: E402  # the shipped witness loader
import iter2_claims_probe_runner as runner  # noqa: E402  # the shipped seam implementations

EVIDENCE_ROOT = REPO_ROOT / "docs" / "evidence" / "C101"
PROVIDERS_DIR = REPO_ROOT / "opendata" / "data" / "providers"
CONTROL_ATTEMPTS = 4
CONTROL_MIN_PASS = 2
SUBJECT = ("yfinance", "EquityQuote")
SUBJECT_HOSTS = ["query1.finance.yahoo.com", "query2.finance.yahoo.com"]
SUBJECT_QUERY = {"symbol": "AAPL"}
TEST_FILE = REPO_ROOT / "tests" / "test_yfinance_iter2_market.py"
SEAM_ATTRS = ("http_transport", "get_shared_http_client")
# Refusals that would mean this experiment is mis-shaped rather than that the leg misses the seam.
PROBE_FAULT_ERRORS = ("TypeError", "KeyError", "AttributeError", "NameError", "WitnessBlocked")
LANDED_TOTAL = 350
LANDED_ACCEPTED = 266
LANDED_IDENTITY_SET = "5168e1e723999312"
DECLARED_EXPECTED = 15


def _block_sockets() -> None:
    """Install the runner's own refusal so a missed seam fails loudly instead of fetching."""
    runner._block_real_sockets()


def _grant_credentials(config: dict[str, Any]) -> list[str]:
    """Apply the runner's fixture-token grant so a credential gate is not read as a seam miss."""
    granted: list[str] = []
    for name, value in config.get("credential_env", {}).items():
        if not os.environ.get(name):
            os.environ[name] = str(value)
            granted.append(name)
    return granted


def _entries_without_client_seam(limit: int) -> list[tuple[str, str, dict[str, Any]]]:
    """Landed witness entries that rely on the engine transport seam, in provider order."""
    picked: list[tuple[str, str, dict[str, Any]]] = []
    for provider in witnesses.providers_with_witness():
        config = witnesses.provider_config(provider)
        for model in sorted(config["models"]):
            try:
                entry = dict(witnesses.model_entry(provider, model))
            except KeyError:
                continue
            if "client_seam" in entry:
                continue
            if not (EVIDENCE_ROOT / "fixtures" / provider / f"{model}.json").is_file():
                continue
            entry["hosts"] = config["allowed_hosts"]
            entry["granted"] = _grant_credentials(config)
            picked.append((provider, model, {"model": model, **entry}))
            break
        if len(picked) >= limit:
            return picked[:limit]
    return picked


def _document_for(provider: str, model: str) -> tuple[Any, str]:
    """The fixture document when the row has a witness module, else an unused placeholder."""
    try:
        return runner._fixture_document(EVIDENCE_ROOT, provider, model), "fixture"
    except runner.WitnessBlocked:
        return {"responses": []}, "placeholder"


def _run_engine_seam(provider: str, model: str, entry: dict[str, Any]) -> tuple[int, int, str, str]:
    """Reproduce ``_run_direct`` inline so the injected transport stays readable on failure."""
    fetcher_type = runner._resolve_fetcher_type(provider, model)
    document, document_kind = _document_for(provider, model)
    transport = runner.FixedResponseTransport(runner.HttpResponse(200, document))
    probe_class = type(
        f"Probed{fetcher_type.__name__}", (fetcher_type,), {"http_transport": transport}
    )
    ctx = runner._grant_context(provider, model, entry["hosts"], sends=runner._DEFAULT_SENDS)
    try:
        rows = list(probe_class().fetch(ctx=ctx, **entry["query"]))
    except BaseException as exc:
        return -1, len(transport.calls), f"{type(exc).__name__}:{str(exc)[:60]}", document_kind
    return len(rows), len(transport.calls), "None", document_kind


def _declared_rows(judged: dict[str, str]) -> list[dict[str, Any]]:
    """Residue rows of the subject provider that the shipped per-row judge reads as declared.

    Before wave 27 no row in the residue published a ``row_model`` that was the domain's contract
    (``readiness_census.py``), so whatever that judge now lists for this provider *is* the edit
    this round made -- no hand-carried identity list is needed to pin it.
    """
    declared: list[dict[str, Any]] = []
    for identity, status in sorted(judged.items()):
        if status == "OFFLINE_ACCEPTED" or not identity.startswith(f"{SUBJECT[0]}::"):
            continue
        face = guard._row(identity, status)
        if face.get("declared"):
            declared.append(face)
    return declared


def _shown(value: object, index: int) -> str:
    """Print one slot of a measurement tuple, or NA when the measurement itself was impossible."""
    if value is None:
        return "NA"
    if isinstance(value, tuple):
        return str(value[index]) if index < len(value) else "NA"
    return str(value)


def _package_seam_reads(provider: str) -> tuple[int, int] | None:
    """Count reads of each seam attribute in one package, or None when the package is absent."""
    directory = PROVIDERS_DIR / provider
    if not directory.is_dir():
        return None
    counts: Counter[str] = Counter()
    for path in sorted(directory.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        read = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        attr = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        for name in SEAM_ATTRS:
            if name in read or name in attr:
                counts[name] += 1
    return counts[SEAM_ATTRS[0]], counts[SEAM_ATTRS[1]]


def _sdk_call_surface(receiver: str = "_sdk") -> list[tuple[str, int]]:
    """``(attribute, smallest positional-argument count)`` per ``<receiver>.<attr>(...)`` call."""
    surface: dict[str, int] = {}
    for path in sorted((PROVIDERS_DIR / SUBJECT[0]).rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            base = node.func.value
            if not (isinstance(base, ast.Name) and base.id == receiver):
                continue
            seen = surface.get(node.func.attr)
            positional = len(node.args)
            surface[node.func.attr] = positional if seen is None else min(seen, positional)
    return sorted(surface.items())


def _installed_seam_arity() -> tuple[int, int] | None:
    """``(parameters, required_parameters)`` of the callable the client seam installs."""
    tree = ast.parse(inspect.getsource(runner._client_seam), filename="runner")
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id != "setattr" or len(node.args) != 3:
            continue
        value = node.args[2]
        if isinstance(value, ast.Lambda):
            return len(value.args.args), len(value.args.args) - len(value.args.defaults)
    return None


def _setattr_patch_points(path: Path) -> list[str]:
    """Names the package's own tests patch on ``_sdk`` / ``_session`` receivers."""
    if not path.is_file():
        return []
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    patched: list[str] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "setattr"
        ):
            continue
        args = node.args
        if len(args) < 2 or not isinstance(args[0], ast.Name):
            continue
        if not isinstance(args[1], ast.Constant):
            continue
        if args[0].id in {"_sdk", "_session"} and isinstance(args[1].value, str):
            patched.append(args[1].value)
    return patched


def main(argv: list[str] | None = None) -> int:
    """Measure the harness's seam surface against what a yfinance fetch leg actually reads."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "--attempts",
        type=int,
        default=CONTROL_ATTEMPTS,
        help="landed engine-transport witness entries to attempt as controls",
    )
    args = parser.parse_args(argv)
    _block_sockets()

    report = seam._load_report()
    judged = {str(row["model_identity"]): str(row["status"]) for row in report["models"]}
    summary = report["summary"]
    inventory = report["inventory"]
    identity_set = str(inventory["expected_identity_set_sha256"])[:16]
    accepted = sum(1 for status in judged.values() if status == "OFFLINE_ACCEPTED")
    face_ok = (
        len(judged) == LANDED_TOTAL
        and accepted == LANDED_ACCEPTED
        and identity_set == LANDED_IDENTITY_SET
        and int(summary["identity_count"]) == len(judged)
        and int(summary["offline_acceptance_count"]) == accepted
    )
    print(
        f"A_landed_report_face total={len(judged)} summary_count={summary['identity_count']} "
        f"accepted={accepted} summary_accepted={summary['offline_acceptance_count']} "
        f"identity_set={identity_set}"
    )
    print(f"A_landed_report_face PASS={face_ok}")

    controls = _entries_without_client_seam(args.attempts)
    control_reads: list[tuple[str, str, int, int, str]] = []
    for provider, model, entry in controls:
        rows, calls, error, document_kind = _run_engine_seam(provider, model, entry)
        control_reads.append((provider, model, rows, calls, error))
        print(
            f"A_engine_transport_control {provider}::{model} rows={rows} "
            f"transport_calls={calls} document={document_kind} "
            f"granted={','.join(entry.get('granted', [])) or 'none'} error={error}"
        )
    passed = sum(1 for _, _, rows, calls, _ in control_reads if rows > 0 and calls > 0)
    arm_a = len(control_reads) == len(controls) and passed >= CONTROL_MIN_PASS
    print(
        f"A_engine_transport_controls attempted={len(control_reads)} passed={passed} "
        f"need={CONTROL_MIN_PASS} PASS={arm_a}"
    )

    subject_rows, subject_calls, subject_error, subject_doc = _run_engine_seam(
        SUBJECT[0], SUBJECT[1], {"hosts": SUBJECT_HOSTS, "query": SUBJECT_QUERY}
    )
    experiment_broke = subject_error.split(":", 1)[0] in PROBE_FAULT_ERRORS
    arm_b = subject_rows == -1 and subject_calls == 0 and not experiment_broke
    print(
        f"B_subject {SUBJECT[0]}::{SUBJECT[1]} rows={subject_rows} "
        f"transport_calls={subject_calls} document={subject_doc} "
        f"probe_fault={experiment_broke} error={subject_error} PASS={arm_b}"
    )

    residue = sorted(
        identity for identity, status in judged.items() if status != "OFFLINE_ACCEPTED"
    )
    rows_by_provider = Counter(identity.partition("::")[0] for identity in residue)
    by_provider = sorted(rows_by_provider)
    census = {provider: _package_seam_reads(provider) for provider in by_provider}
    for provider in by_provider:
        print(
            f"C_seam_reads provider={provider} rows={rows_by_provider[provider]} "
            f"http_transport={_shown(census[provider], 0)} "
            f"shared_client={_shown(census[provider], 1)}"
        )
    subject_reads = census.get(SUBJECT[0])
    transport_holders = sum(1 for value in census.values() if value and value[0] > 0)
    client_holders = sum(1 for value in census.values() if value and value[1] > 0)
    arm_c = (
        subject_reads == (0, 0)
        and transport_holders > 0
        and client_holders > 0
        and len(census) == len(by_provider)
    )
    print(
        f"C_static_seam_attribute_census providers={len(census)} subject={subject_reads} "
        f"http_transport_holders={transport_holders} shared_client_holders={client_holders} "
        f"PASS={arm_c}"
    )

    arity = _installed_seam_arity()
    required = arity[1] if arity else -1
    patched = _setattr_patch_points(TEST_FILE)
    sdk_patches = sorted({name for name in patched if name.startswith("require_")})
    arm_d = arity is not None and required == 0 and bool(sdk_patches)
    print(
        f"D_harness_surface installed=lambda{_shown(arity, 0)}{_shown(arity, 1)} "
        f"required_args={required} seam_kinds={list(witnesses._CLIENT_SEAM_KINDS)} "
        f"test_patched_sdk_callables={len(sdk_patches)} sorted={sdk_patches[:6]} PASS={arm_d}"
    )

    surface = _sdk_call_surface()
    min_positional = min((count for _, count in surface), default=-1)
    arm_f = bool(surface) and min_positional >= 1
    print(
        f"F_sdk_call_surface receiver=_sdk call_sites={len(surface)} "
        f"min_positional_args={min_positional} names={surface[:4]} PASS={arm_f}"
    )

    declared = _declared_rows(judged)
    for face in declared:
        print(
            f"E_declared {face['identity']} fetcher={face['fetcher']} "
            f"domain={face['domain']} contract={face['contract']} "
            f"row_model_is_contract={face['declared_is_contract']} "
            f"witness_entry={face['witness_entry']}"
        )
    is_contract = sum(1 for face in declared if face["declared_is_contract"])
    arm_e = len(declared) == DECLARED_EXPECTED and is_contract == len(declared)
    print(
        f"E_declaration_face provider={SUBJECT[0]} declared={len(declared)} "
        f"expected={DECLARED_EXPECTED} row_model_is_contract={is_contract} PASS={arm_e}"
    )

    moved = dc._residue_needs_declare(report)
    priced = [dc._price(identity) for identity in moved]
    tags = Counter(dc._tag(row) for row in priced)
    declared_left = sum(1 for row in priced if row.get("declared"))
    print(
        f"pricer_face needs_declare={len(moved)} tags={dict(sorted(tags.items()))} "
        f"declared_inside_population={declared_left}"
    )

    arms = [face_ok, arm_a, arm_b, arm_c, arm_d, arm_f, arm_e]
    passed_arms = sum(1 for ok in arms if ok)
    print(
        f"seam_kind_face residue={len(residue)} subject_transport_calls={subject_calls} "
        f"subject_error={subject_error.split(':')[0]} controls={len(control_reads)} "
        f"declared={len(declared)} arms={len(arms)} arms_passed={passed_arms} "
        f"arms_failed={len(arms) - passed_arms} identity_set={identity_set}"
    )
    return 0 if passed_arms == len(arms) else 1


if __name__ == "__main__":
    raise SystemExit(main())
