"""Decide, per row, whether the 7 ``INCOMPLETE`` identities are convertible by a witness wave.

Section 87.8 armed this batch on a count (7) and predicted plane 1 266 -> 273. The count is a
judge status class, not a work list: an ``INCOMPLETE`` row is one whose five phases were
*recorded* as BLOCKED, so it differs from a ``NEED_BINDING`` row only in that a blocked claim
already exists. Four separate questions have to be answered per row before anything is edited,
and each one has been answered incorrectly in the past by copying the answer from a coarser
screen:

1. ``bound`` -- does ``ProviderRegistry.resolve_model`` return a fetcher for this identity? A row
   with no fetcher has no ``transform_data`` to open, so the second axis cannot even be posed.
2. ``ruled_out`` -- does the provider's own ledger dict (spelled ``NOT_DECLARABLE`` in cboe and
   intrinio, ``NOT_DECLARED`` in econdb) carry the identity? The spelling differs, so the probe
   takes any module attribute whose name starts with ``NOT_DECL`` and whose keys contain the
   model, instead of hardcoding one name -- a hardcoded name is how a guard goes mute. A missing
   ``specs`` module is **not** a ledger hit: yfinance has no ``specs.py`` at all, and reading "no
   ledger exists" as "this row is ruled out" made one boolean carry two opposite causes, so the
   two are returned and printed apart (``ruled_out`` / ``no_ledger``).
3. ``guard`` -- *this row's own* fetcher class must hold a refusal a fixture-document tamper can
   trip. The shipped census buckets by method name and stops there, which is not enough:
   ``commodity_psd_report.py:104`` (checks the bytes that came back) and
   ``weather_bulletin_download.py:112`` (checks the URL before any request) are both
   ``extract_data``, and only one of them is document-trippable. This probe keeps the shipped
   buckets and adds one measured relation -- the raise sits *after* a governed-client call inside
   the same function. The client call is found by receiver, and the receiver names are read off
   the module (imports of a ``_client`` module, names assigned from the seam factory), never from a
   hardcoded alias list: attribute-name matching alone invented transport calls in
   ``federal_reserve/models/fomc_documents.py``, where ``transform_data`` matches the verb list
   twice on a BeautifulSoup tag -- ``anchor.get("href")`` at ``:111`` and ``anchor.get_text(...)``
   at ``:134`` -- and the first of them is what makes the raises at ``:113`` and ``:127`` read
   document-trippable (``_transport_line`` takes ``min``, not ``max``).
4. ``row_class`` -- *and* the class the bound fetcher constructs in its own body has to **be**
   ``contract_model(domain)``, not merely be shaped like it. A row that passes 1-3 and fails this
   one is not a witness wave: the declaration the wave would write is refused either by
   ``tests/test_federal_reserve_iter2_models.py:382-402`` (if it names the built class) or by the
   service leg, which revalidates every row against ``contract_model(domain)``.

Verdicts, in decreasing strength:

``DOCUMENT_REACHABLE``
    a code-carrying raise in ``transform_data``, which the engine calls with the decoded payload
    (``_engine/http_json.py:1462``), so a tamper reaches it by construction.
``DOCUMENT_TRIPPED``
    a code-carrying raise in ``extract_data`` on a line after that function's client call.
``ROW_CLASS_NOT_CONTRACT``
    the guard axis is met, but the bound fetcher builds a class that is not
    ``contract_model(domain)`` -- the two federal_reserve rows this probe was written to price.
    It outranks the guard verdicts because it decides *which kind of work* the row is, and the run
    that armed wave 26 did not measure it at all.
``QUERY_ONLY``
    code-carrying sites exist, none of them document-trippable.
``NO_CODE_SITE``
    the class raises, but no raise carries a SCREAMING_SNAKE code literal, so the shipped
    classifier sees nothing. Printed apart from ``QUERY_ONLY`` because it says what a counterfact
    could cite, not that guards are absent.
``UNBOUND``
    no fetcher resolved; the question was never posed.

Arms, all six of which can fail:

``A_landed_positive``
    the two rows wave 25 landed (identities read out of the report, not typed as a claim about
    the future) must each read a document-trippable verdict *and* be ``OFFLINE_ACCEPTED``. The
    PSD report row is the one that refuted this probe's first draft, which demanded
    ``transform_data`` and so read an accepted row as un-witnessable; that face is kept in
    ``runs/wave26-step0-second-axis.txt`` instead of being edited out.
``B_landed_negative``
    ``government_us::WeatherBulletinDownload`` must read neither document-trippable verdict.
``C_class_scoped``
    for the same control, the class-scoped strict count must be 0 while the provider-package
    screen is > 0, so a per-row ``td`` cannot be the provider screen copied onto a row.
``D_bucket_reconciliation``
    the shipped ``_module_sites`` and this probe's per-function walk must agree on the
    code-carrying raise count for every class it reads. If they diverge, the finer reading sits
    on a different classifier than the census's, and that has to fail here rather than in a
    later wave.
``E_transport_predicate_discriminates``
    across every ``extract_data`` code site the probe read, the new client-call relation has to
    take both values. A predicate that always answered True would hand ``DOCUMENT_TRIPPED`` to the
    URL guard the negative control is famous for, so the arm is the one thing standing between the
    verdict and a restatement of the method name.
``F_row_class_identity``
    three-sided, because this is the axis that was missing. Every ``OFFLINE_ACCEPTED`` row that
    declares a ``row_model`` must read ``row_model is contract_model(domain)`` (the accepted set is
    read out of the report, none of it typed here); the domain's contract class compared with
    itself must read ``match``; and the two ``FederalReserveFomcDocument`` classes that really
    exist in this repository -- one per module, same name -- must read ``no`` for the macro twin.
    An identity test that cannot tell those two apart would have priced this batch as convertible
    for the second time.

Exits non-zero when an arm fails. A constant verdict across the batch prints ``movable=False``
and is not a gate, because all-seven-same is a legitimate finding about the population.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import declare_candidates as dc  # the shipped construction reader (AST)
import post_response_guard_census as guard  # the shipped site classifier
import seam_surface_match as seam  # the shipped report reader

sys.path.insert(0, str(seam.REPO_ROOT / "scripts" / "quality"))

import iter2_claims_fixtures as witnesses  # the repo's own shard loader

from opendata.data.domains import contract_model
from opendata.data.models.base import ContractModel
from opendata.data.providers.catalog import register_provider
from opendata.data.registry import ProviderRegistry

VERDICTS = (
    "DOCUMENT_REACHABLE",
    "DOCUMENT_TRIPPED",
    "ROW_CLASS_NOT_CONTRACT",
    "QUERY_ONLY",
    "NO_CODE_SITE",
    "UNBOUND",
)
TRIPPABLE = ("DOCUMENT_REACHABLE", "DOCUMENT_TRIPPED")
CONTROLS_POSITIVE = ("government_us::CommodityPsdData", "government_us::CommodityPsdReport")
CONTROL_NEGATIVE = "government_us::WeatherBulletinDownload"
TRANSPORT_ATTRS = ("get_bytes", "get_json", "get_text", "download", "request", "post", "get")
CLIENT_MODULE_SEGMENT = "_client"
SEAM_FACTORY = "get_shared_http_client"
BUCKETS = ("transform_data", "extract_data", "other")


def _fetcher(provider: str, model: str) -> tuple[Any, str]:
    """Return (fetcher or None, resolve error or "") through the shipped registry."""
    registry = ProviderRegistry()
    register_provider(provider, registry)
    try:
        return registry.resolve_model(provider, model), ""
    except Exception as exc:  # the error *name* is the measurement here
        return None, f"{type(exc).__name__}:{exc}"


def _ruled_out(provider: str, model: str) -> tuple[str, str]:
    """Return (ledger entry carrying this identity, why no ledger could be read).

    These were one string until the widened run printed ``ruled_out=True`` for 26 yfinance rows that
    no ledger rules out -- yfinance ships no ``specs.py`` -- because a "no module" message was
    truthy. A boolean with two opposite causes cannot gate a wave, so the causes come back apart.
    """
    try:
        specs = importlib.import_module(f"opendata.data.providers.{provider}.specs")
    except Exception as exc:  # no specs module is a reading, not a crash
        return "", f"NO_SPECS_MODULE:{type(exc).__name__}"
    for name, value in vars(specs).items():
        if not name.startswith("NOT_DECL") or not isinstance(value, dict):
            continue
        if model in value:
            return f"{name}:{str(value[model])[:70]}", ""
    return "", ""


def _client_names(tree: ast.Module) -> set[str]:
    """Names this module can reach the governed client through, read off the module itself.

    Hardcoding the aliases would mute the predicate the day a package binds the client under a
    name not on the list, which is the failure this instrument is meant to catch in others. So:
    an import of a ``_client`` module (under any alias), and any name assigned from an expression
    that calls ``get_shared_http_client`` -- including the ``self._http_client or ...`` shape.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[-1] == CLIENT_MODULE_SEGMENT or CLIENT_MODULE_SEGMENT in (
                    alias.asname or ""
                ):
                    names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == CLIENT_MODULE_SEGMENT:
                    names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Assign) and _calls_seam_factory(node.value):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
    return names


def _calls_seam_factory(value: ast.expr) -> bool:
    return any(
        isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == SEAM_FACTORY)
            or (isinstance(node.func, ast.Attribute) and node.func.attr == SEAM_FACTORY)
        )
        for node in ast.walk(value)
    )


def _is_client_receiver(target: ast.Attribute, names: set[str]) -> bool:
    """Is this ``x.verb(...)`` call made on the client, rather than on a payload object?"""
    base = target.value
    if isinstance(base, ast.Name):
        return base.id in names
    if isinstance(base, ast.Attribute):
        return base.attr.endswith(CLIENT_MODULE_SEGMENT) or base.attr in {"client", "http"}
    return _calls_seam_factory(base)


def _transport_line(func: ast.AST, names: set[str]) -> int:
    """Line of the first governed-client call in ``func``, or 0 if the function makes none.

    Two over-claims were measured and rejected. Matching the attribute name alone reads ``get`` as
    ``dict.get``; requiring the receiver but not the verb reads ``_client.array_rows`` as a request.
    A site counts only when both hold, and that is what makes the reading a floor: a package that
    fetches through a module helper (``fomc_documents.py:100`` calls ``get_text(url, ctx.timeout)``)
    reports ``client_call_at=0`` here, so ``DOCUMENT_TRIPPED`` can under-claim and never over-claim.
    """
    lines: list[int] = []
    for node in ast.walk(func):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        target = node.func
        if target.attr in TRANSPORT_ATTRS and _is_client_receiver(target, names):
            lines.append(node.lineno)
    return min(lines) if lines else 0


def _nearest_test(node: ast.Raise, parents: dict[ast.AST, ast.AST]) -> str:
    """The condition the raise sits under, so the reader sees what the guard actually tests."""
    child: ast.AST = node
    while (parent := parents.get(child)) is not None:
        if isinstance(parent, ast.If):
            return ast.unparse(parent.test)
        child = parent
    return "<unconditional>"


def _row_class_face(fetcher: object, contract: type | None) -> dict[str, Any]:
    """Third axis: does the bound fetcher's serving path ever construct the domain contract?

    ``opendata/data/providers/federal_reserve/models/fomc_documents.py:142`` builds
    ``federal_reserve_macro.FederalReserveFomcDocument`` while
    ``contract_model('federal_reserve_fomc_documents')`` resolves to the same *name* in
    ``federal_reserve_clean_documents``: the two classes share one field only. ``yield_curve.py``
    is the milder twin -- ``federal_reserve_macro.FederalReserveYieldCurve`` has the contract's
    three fields under a different name. Both read ``is_contract=False`` here, and both pass the
    guard axis, which is exactly why the guard axis alone mis-priced this batch as convertible.

    The consequence is not cosmetic. ``tests/test_federal_reserve_iter2_models.py:382-402``
    requires ``row_model is contract_model(domain)`` for any declaring fetcher, and
    ``provider_model_query._serialize_contract_row`` revalidates every row against
    ``contract_model(domain)`` -- so declaring the contract class over macro-built rows passes the
    direct leg and refuses on the service leg, and declaring the built class trips the frozen test.
    Converting such a row is an identity edit to the serving path, not a declaration.
    """
    if contract is None:
        return {"is_contract": False, "emitted": [], "look_alike": False, "no_contract": True}
    emitted: set[str] = set()
    matches = False
    for klass in type(fetcher).__mro__:
        module_name = str(klass.__module__)
        if not module_name.startswith("opendata."):
            continue
        module = sys.modules.get(module_name)
        path = inspect.getsourcefile(klass)
        if module is None or path is None:
            continue
        tree = ast.parse(Path(path).read_text(encoding="utf-8"), filename=path)
        helpers = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
        owners = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef) and node.name == klass.__qualname__.split(".")[0]
        ]
        built: set[str] = set()
        called: set[str] = set()
        for owner in owners:
            names, hits = dc._constructed(owner)
            built |= names
            called |= hits
        for name in called:
            helper = helpers.get(name)
            if helper is not None:
                built |= dc._constructed(helper)[0]
        for name in sorted(built):
            obj = getattr(module, name, None)
            if not (isinstance(obj, type) and issubclass(obj, ContractModel)):
                continue
            emitted.add(f"{obj.__module__}.{obj.__qualname__}")
            if obj is contract:
                matches = True
    return {
        "is_contract": matches,
        "emitted": sorted(emitted),
        "look_alike": any(name.rsplit(".", 1)[-1] == contract.__name__ for name in emitted)
        and not matches,
    }


def _class_sites(fetcher: object) -> dict[str, Any]:
    """Raise sites of the fetcher's own in-repo classes, with the transport-order relation added."""
    counts: Counter[str] = Counter()
    trippable: list[str] = []
    doc_sites: list[dict[str, Any]] = []
    raises = 0
    code_raises = 0
    shipped_sites = 0
    files: list[str] = []
    client_names: set[str] = set()
    for klass in type(fetcher).__mro__:
        if not str(klass.__module__).startswith("opendata."):
            continue
        path = inspect.getsourcefile(klass)
        if path is None:
            continue
        source = Path(path).resolve()
        rel = (
            str(source.relative_to(seam.REPO_ROOT))
            if source.is_relative_to(seam.REPO_ROOT)
            else str(source)
        )
        files.append(rel)
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=rel)
        names = _client_names(tree)
        client_names |= names
        owner = klass.__qualname__.split(".")[0]
        shipped_sites += sum(1 for site in guard._module_sites(tree, rel) if site["class"] == owner)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or node.name != owner:
                continue
            parents: dict[ast.AST, ast.AST] = {}
            for parent in ast.walk(node):
                for child in ast.iter_child_nodes(parent):
                    parents[child] = parent
            methods = [
                f for f in node.body if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            for method in methods:
                transport = _transport_line(method, names)
                for raise_node in guard._own_raises(method):
                    raises += 1
                    code = guard._first_literal(raise_node)
                    if not code:
                        continue
                    code_raises += 1
                    bucket = method.name if method.name in guard.DOCUMENT_METHODS else "other"
                    counts[bucket] += 1
                    after = bool(transport) and raise_node.lineno > transport
                    if bucket == "transform_data" or (bucket == "extract_data" and after):
                        trippable.append(f"{rel}:{raise_node.lineno} {method.name} {code}")
                    if bucket in guard.DOCUMENT_METHODS:
                        doc_sites.append(
                            {
                                "where": f"{rel}:{raise_node.lineno}",
                                "method": method.name,
                                "code": code,
                                "transport_at": transport,
                                "after": after,
                                "test": _nearest_test(raise_node, parents)[:58],
                            }
                        )
    return {
        "counts": dict(counts),
        "trippable": trippable,
        "doc_sites": doc_sites,
        "raises": raises,
        "code_raises": code_raises,
        "shipped_sites": shipped_sites,
        "files": files,
        "client_names": sorted(client_names),
    }


def _verdict(measured: dict[str, Any]) -> str:
    counts = measured["counts"]
    if not measured.get("declared") and not measured.get("is_contract"):
        return "ROW_CLASS_NOT_CONTRACT"
    if counts.get("transform_data"):
        return "DOCUMENT_REACHABLE"
    if measured["trippable"]:
        return "DOCUMENT_TRIPPED"
    if counts:
        return "QUERY_ONLY"
    return "NO_CODE_SITE" if measured["raises"] else "UNBOUND"


def _entry(provider: str, model: str) -> dict[str, Any] | None:
    try:
        entry: dict[str, Any] = witnesses.model_entry(provider, model)
        return entry
    except KeyError:
        return None


def _row(identity: str, status: str) -> dict[str, Any]:
    """Measure every column the wave-sizing decision is made from."""
    provider, _, model = identity.partition("::")
    face: dict[str, Any] = {
        "identity": identity,
        "status": status,
        "witness_entry": bool(_entry(provider, model)),
    }
    fetcher, error = _fetcher(provider, model)
    face["bound"] = fetcher is not None
    face["resolve_error"] = error
    face["ruled_out"], face["no_ledger"] = _ruled_out(provider, model)
    if fetcher is None:
        face["verdict"] = "UNBOUND"
        return face
    face["fetcher"] = type(fetcher).__name__
    row_model = getattr(fetcher, "row_model", None)
    face["declared"] = isinstance(row_model, type) and issubclass(row_model, ContractModel)
    face["canonical_model"] = getattr(fetcher, "canonical_model", None)
    domain = getattr(getattr(fetcher, "capability", None), "domain", None)
    face["domain"] = domain
    try:
        expected = contract_model(domain) if domain else None
    except LookupError:
        expected = None
        face["contract_error"] = f"NO_CONTRACT_FOR:{domain}"
    face["contract"] = getattr(expected, "__name__", None)
    face["declared_is_contract"] = row_model is expected
    face.update(_row_class_face(fetcher, expected))
    face.update(_class_sites(fetcher))
    face["verdict"] = _verdict(face)
    return face


def _provider_screen(provider: str) -> dict[str, int]:
    """The coarser number this probe must not be confused with: sites anywhere in the package."""
    buckets = guard._package_sites(provider)["buckets"]
    return {k: len(v) for k, v in buckets.items()}


def _print_row(row: dict[str, Any], screen: dict[str, int]) -> None:
    counts = row.get("counts", {})
    print(
        f"row {row['identity']} status={row['status']} bound={row['bound']} "
        f"ruled_out={bool(row['ruled_out'])} no_ledger={row.get('no_ledger') or '-'} "
        f"witness_entry={row['witness_entry']} "
        f"declared={row.get('declared', False)} fetcher={row.get('fetcher', '-')} "
        f"verdict={row['verdict']} class_counts={ {k: counts.get(k, 0) for k in BUCKETS} } "
        f"raises={row.get('raises', 0)} code_raises={row.get('code_raises', 0)} "
        f"package_screen={ {k: screen.get(k, 0) for k in BUCKETS} }"
    )
    for site in row.get("doc_sites", []):
        print(
            f"    doc_site {row['identity']} {site['where']} {site['method']} {site['code']} "
            f"client_call_at={site['transport_at']} raise_after_client={site['after']} "
            f"test={site['test']}"
        )
    for note in row.get("trippable", []):
        print(f"    trippable {note}")
    if row["ruled_out"] or row.get("no_ledger"):
        print(
            f"    ruled_out_detail={row['ruled_out']} no_ledger={row.get('no_ledger') or '-'} "
            f"resolve_error={row.get('resolve_error') or '-'}"
        )
    if not row["bound"]:
        return
    print(
        f"    domain={row.get('domain')} canonical={row.get('canonical_model')} "
        f"client_names={row.get('client_names')} files={row.get('files')}"
    )
    print(
        f"    row_class {row['identity']} contract={row.get('contract')} "
        f"is_contract={row.get('is_contract')} look_alike={row.get('look_alike')} "
        f"declared_is_contract={row.get('declared_is_contract')} "
        f"emitted={row.get('emitted')}"
    )


def _identity_read(left: type | None, right: type | None) -> str:
    """``match`` / ``look_alike_name`` / ``no`` -- class identity first, name only second."""
    if left is None or right is None:
        return "no"
    if left is right:
        return "match"
    return "look_alike_name" if left.__name__ == right.__name__ else "no"


def _accepted_sample(judged: dict[str, str], stride: int = 40) -> list[str]:
    """Every ``stride``-th accepted identity under the sort key ``sorted(identities)``.

    Arm F needs accepted rows, and measuring all of them would make the probe a two-minute run, so
    the sample is a fixed stride over a named sort key rather than a hand-picked list: a hand-picked
    list is how a control stops covering the population it claims to.
    """
    accepted = sorted(i for i, s in judged.items() if s == "OFFLINE_ACCEPTED")
    return accepted[::stride]


def main() -> int:
    """Print the per-row table, then gate the instrument on its landed controls."""
    report = seam._load_report()
    judged = {str(row["model_identity"]): str(row["status"]) for row in report["models"]}
    incomplete = sorted(i for i, s in judged.items() if s == "INCOMPLETE")
    screens: dict[str, dict[str, int]] = {}

    rows = [_row(i, judged[i]) for i in incomplete]
    for row in rows:
        provider = str(row["identity"]).partition("::")[0]
        screens.setdefault(provider, _provider_screen(provider))
        _print_row(row, screens[provider])

    verdicts = Counter(str(row["verdict"]) for row in rows)
    convertible = [str(row["identity"]) for row in rows if str(row["verdict"]) in TRIPPABLE]

    controls = [_row(i, judged.get(i, "?")) for i in CONTROLS_POSITIVE]
    arm_a = True
    for control in controls:
        identity = str(control["identity"])
        ok = control["verdict"] in TRIPPABLE and judged.get(identity) == "OFFLINE_ACCEPTED"
        arm_a = arm_a and ok
        print(
            f"A_landed_positive {identity} verdict={control['verdict']} "
            f"judge_status={judged.get(identity)} counts={control.get('counts')} "
            f"raises={control.get('raises')} code_raises={control.get('code_raises')} PASS={ok}"
        )

    neg = _row(CONTROL_NEGATIVE, judged.get(CONTROL_NEGATIVE, "?"))
    neg_provider = CONTROL_NEGATIVE.partition("::")[0]
    screen = screens.setdefault(neg_provider, _provider_screen(neg_provider))
    arm_b = neg["verdict"] not in TRIPPABLE
    print(
        f"B_landed_negative {CONTROL_NEGATIVE} verdict={neg['verdict']} "
        f"class_counts={neg.get('counts')} raises={neg.get('raises')} "
        f"code_raises={neg.get('code_raises')} PASS={arm_b}"
    )
    _print_row(neg, screen)

    class_td = int(neg.get("counts", {}).get("transform_data", 0))
    arm_c = class_td == 0 and screen.get("transform_data", 0) > 0
    print(
        f"C_class_scoped {CONTROL_NEGATIVE} class_td={class_td} "
        f"package_td={screen.get('transform_data', 0)} PASS={arm_c}"
    )

    drift = [
        f"{row['identity']} walk={row.get('code_raises')} shipped={row.get('shipped_sites')}"
        for row in [*rows, *controls, neg]
        if row["bound"] and row.get("code_raises") != row.get("shipped_sites")
    ]
    arm_d = not drift
    print(
        f"D_bucket_reconciliation rows_checked={len(rows) + len(controls) + 1} "
        f"divergent={len(drift)} divergent_rows={drift} PASS={arm_d}"
    )

    extract_sites = [
        (str(row["identity"]), site)
        for row in [*rows, *controls, neg]
        for site in row.get("doc_sites", [])
        if site["method"] == "extract_data"
    ]
    both = {site["after"] for _identity, site in extract_sites}
    arm_e = both == {True, False}
    after_counts = {
        value: sum(1 for _i, site in extract_sites if site["after"] is value)
        for value in (True, False)
    }
    print(
        f"E_transport_predicate_discriminates extract_data_sites={len(extract_sites)} "
        f"after_counts={after_counts} readings_seen={sorted(both)} PASS={arm_e}"
    )

    sample = _accepted_sample(judged)
    bad_identity: list[str] = []
    declaring = 0
    for identity in sample:
        measured = _row(identity, judged[identity])
        if not measured["bound"] or not measured.get("declared"):
            continue
        declaring += 1
        if not measured.get("declared_is_contract"):
            bad_identity.append(f"{identity} row_model={measured.get('contract')}")
    macro_twin = importlib.import_module("opendata.data.models.federal_reserve_macro")
    clean_twin = importlib.import_module("opendata.data.models.federal_reserve_clean_documents")
    fomc_contract = contract_model("federal_reserve_fomc_documents")
    self_read = _identity_read(fomc_contract, fomc_contract)
    twin_read = _identity_read(
        fomc_contract, getattr(macro_twin, "FederalReserveFomcDocument", None)
    )
    curve_read = _identity_read(
        contract_model("federal_reserve_yield_curve"),
        getattr(macro_twin, "FederalReserveYieldCurve", None),
    )
    clean_twin_present = getattr(clean_twin, "FederalReserveFomcDocument", None) is fomc_contract
    arm_f = (
        not bad_identity
        and declaring > 0
        and self_read == "match"
        and twin_read == "look_alike_name"
        and curve_read == "no"
        and clean_twin_present
    )
    print(
        f"F_row_class_identity accepted_sampled={len(sample)} declaring={declaring} "
        f"declaration_not_contract={len(bad_identity)} bad={bad_identity} "
        f"self_read={self_read} macro_fomc_twin_read={twin_read} "
        f"macro_curve_twin_read={curve_read} PASS={arm_f}"
    )

    arms = [arm_a, arm_b, arm_c, arm_d, arm_e, arm_f]
    passed = sum(1 for ok in arms if ok)
    off_enum = sorted({str(row["verdict"]) for row in rows} - set(VERDICTS))
    print(
        f"second_axis_face incomplete={len(rows)} verdicts={dict(sorted(verdicts.items()))} "
        f"convertible={len(convertible)} convertible_rows={convertible} "
        f"movable={len(verdicts) > 1} verdict_enum_violations={off_enum} "
        f"arms={len(arms)} arms_passed={passed} arms_failed={len(arms) - passed} "
        f"identity_set={str(report['inventory']['expected_identity_set_sha256'])[:16]}"
    )
    return 0 if passed == len(arms) else 1


if __name__ == "__main__":
    raise SystemExit(main())
