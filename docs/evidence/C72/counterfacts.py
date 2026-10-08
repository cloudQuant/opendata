"""C72 counterfact harness: seven faces, each with an arm that must pass and an arm that must fail.

A fix recorded only as "the suite passes" is a口头判定. Every face here drives one of the
repository's own judges -- ``ProviderRegistry.resolve``/``resolve_domain``, the declarative engine's
real fetch path, ``provider_model_query._serialize_contract_row``, the
``scripts/quality/declaration_provenance`` audit, and the
``scripts/quality/openbb_inventory_plane`` baseline plane -- and refuses to report OK unless the
*opposite* arm also lands. A face whose should-fail arm passes is printed FAIL and makes the harness
exit 1, because a judge that cannot be shown to differ is not measuring anything.

Run from the repository root: ``python3 docs/evidence/C72/counterfacts.py``
"""

from __future__ import annotations

import importlib
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from opendata.data.capability import Capability
from opendata.data.domains import contract_model, load_domains
from opendata.data.providers._engine import http_json
from opendata.data.providers._engine.http_json import (
    HttpResponse,
    ProviderEngineError,
    build_row_model,
    make_http_json_fetcher,
)
from opendata.data.providers._engine.spec import ColumnSpec, ModelSpec
from opendata.data.providers._engine.testing import (
    SequencedResponseTransport,
    fixture_context,
    synthetic_page,
    synthetic_record,
    valid_query_kwargs,
)
from opendata.data.providers.catalog import engine_declared_models, register_providers, registration_order
from opendata.data.registry import ProviderRegistry
from opendata.services.provider_model_query import (
    ProviderModelQueryOutputError,
    _serialize_contract_row,
)
from scripts.quality.declaration_provenance import (
    VIOLATIONS,
    Finding,
    domain_disagree,
    registered_domains,
    run_self_test,
)
from scripts.quality import openbb_inventory_plane as plane

#: The one provider whose declarations are engine-native in this tree, so the faces below can name a
#: real host and a real contract instead of a fixture.
CBOE = "cboe"


def spec_of(source: str, model: str) -> ModelSpec:
    """The live declaration for one model, read from the runtime rather than copied here."""
    for declared_source, spec in engine_declared_models():
        if declared_source == source and spec.model == model:
            return spec
    raise AssertionError(f"{source}::{model} is not declared in this tree")


def build(source: str, spec: ModelSpec, *pages: HttpResponse) -> tuple[Any, Any]:
    """A fetcher for ``spec`` served by ``pages`` in order, plus the transport that recorded it."""
    transport = SequencedResponseTransport(*pages)
    fetcher_type = make_http_json_fetcher(source, spec)
    fetcher_type.http_transport = transport
    return fetcher_type(), transport


def rows_for(source: str, spec: ModelSpec) -> list[Any]:
    """Fetch one synthetic page through the real engine and return the published rows."""
    fetcher, _ = build(source, spec, synthetic_page(spec, 1))
    return list(
        fetcher.fetch(  # type: ignore[attr-defined]
            ctx=fixture_context(source, spec, sends=2), **valid_query_kwargs(spec)
        )
    )


def codes(findings: list[Finding]) -> set[str]:
    """The violation codes a list of findings carries."""
    return {finding.code for finding in findings if finding.code in VIOLATIONS}


def registration_rows(lines: list[str]) -> list[tuple[int, int, str]]:
    """Index ``docs/data-rights-registry.md`` §1 the way the plane's own reader does.

    Only §1 counts -- §2 is a 待办 table whose first column is also numbered, and treating a
    todo sentence as a data source is how a rights rule becomes unfalsifiable.
    """
    rows: list[tuple[int, int, str]] = []
    in_section = False
    for index, line in enumerate(lines):
        if line.startswith("## "):
            in_section = line.startswith("## 1")
            continue
        if not in_section:
            continue
        match = plane.RIGHTS_TABLE_ROW.match(line.strip())
        if not match:
            continue
        cells = [cell.strip() for cell in match.group("cells").split("|")]
        if cells:
            rows.append((index, int(match.group("number")), cells[0]))
    return rows


def face_resolve_domain() -> tuple[bool, str]:
    """F1 -- an explicit source is routed by its own asset class, not by the domain's first leg.

    Two sources may register one domain under different asset classes (``ths`` serves ``instrument``
    as ``metadata``; a catalog provider serves it as ``index``). The defect asked ``resolve`` for the
    first leg's asset class together with the asked-for source, so the second leg's own request
    matched nothing. No two live sources collide on one domain today, so the pair is registered
    synthetically -- the judge under test is still the real ``resolve``/``resolve_domain``.
    """
    registry = ProviderRegistry()
    legs = (("index", "probe_a"), ("metadata", "probe_b"))
    for asset_class, source in legs:
        capability = Capability(
            asset_class=asset_class,
            domain="probe_shared_domain",
            period="snapshot",
            market="us",
            source=source,
            verified=False,
        )
        registry.register(SimpleNamespace(capability=capability))

    routed = registry.resolve_domain("probe_shared_domain", source="probe_b")
    if routed.capability.asset_class != "metadata" or routed.capability.source != "probe_b":
        return False, f"resolve_domain routed {routed.capability!r}"

    # The pre-fix formula: the asset class off the first leg, kept together with the asked source.
    first_leg = next(iter(registry._fetchers.values()))  # noqa: SLF001 - reproducing the old body
    try:
        registry.resolve(first_leg.capability.asset_class, "probe_shared_domain", source="probe_b")
    except LookupError as error:
        return True, (
            "both legs route by their own asset class; the old first-leg formula still raises "
            f"{type(error).__name__}({error})"
        )
    return False, "the pre-fix formula no longer raises, so this face proves nothing"


def face_rejected_parameter_names() -> tuple[bool, str]:
    """F2 -- a refused query names every undeclared key, in a set the caller can act on.

    Positive arm: a query of only declared parameters returns rows. Counterfact arm: two invented keys
    must both appear in ``ProviderEngineError.rejected`` -- a single hardcoded name in the message
    would let a face like this pass while telling the caller nothing about which key to fix.
    """
    spec = spec_of(CBOE, "IndexConstituents")
    rows = rows_for(CBOE, spec)
    if len(rows) != 1:
        return False, f"valid query published {len(rows)} rows"

    fetcher, _ = build(CBOE, spec, synthetic_page(spec, 1))
    try:
        fetcher.fetch(  # type: ignore[attr-defined]
            ctx=fixture_context(CBOE, spec, sends=2),
            **valid_query_kwargs(spec),
            probe_absent_one="x",
            probe_absent_two="y",
        )
    except ProviderEngineError as error:
        rejected = list(getattr(error, "rejected", ()))
        if rejected != ["probe_absent_one", "probe_absent_two"]:
            return False, f"rejected={rejected} code={error.code}"
        if not str(error).endswith("rejected=['probe_absent_one', 'probe_absent_two']"):
            return False, f"the rendered message lost the keys: {error}"
        return True, f"code={error.code} rejected={rejected}; a valid query served 1 row"
    return False, "an undeclared parameter was accepted"


def face_contract_row_class() -> tuple[bool, str]:
    """F3 -- the engine publishes the domain's contract class, and only that class is served.

    Positive arm: every engine-declared model's rows are ``type(row) is contract_model(domain)``, and
    the real serializer accepts them. Counterfact arm: a subclass carrying identical field values is
    refused by the same serializer, and a declaration whose columns differ from a reviewed domain's
    contract cannot be built at all.
    """
    served: list[str] = []
    for source, spec in engine_declared_models():
        domain = load_domains()[spec.domain]
        contract_type = contract_model(spec.domain)
        rows = rows_for(source, spec)
        if not all(type(row) is contract_type for row in rows):
            return False, f"{source}::{spec.model} published {[type(r).__name__ for r in rows]}"
        if not domain.semantics_declared:
            return False, f"{spec.domain} routed a contract row without declared semantics"
        _serialize_contract_row(rows[0], contract_type)
        served.append(f"{source}::{spec.model}->{contract_type.__name__}")

    spec = spec_of(CBOE, "AvailableIndices")
    contract_type = contract_model(spec.domain)
    look_alike_type = type("LookAlike", (contract_type,), {})
    look_alike = look_alike_type.model_validate(rows_for(CBOE, spec)[0].model_dump(mode="python"))
    try:
        _serialize_contract_row(look_alike, contract_type)
    except ProviderModelQueryOutputError:
        pass
    else:
        return False, "the serializer accepted a look-alike subclass"

    try:
        build_row_model(replace(spec, columns=(*spec.columns, ColumnSpec("probe_invented", "str"))))
    except ValueError as error:
        if "probe_invented" not in str(error):
            return False, f"the column-mismatch refusal lost the name: {error}"
        return True, f"{' ,'.join(served)}; look-alike refused; mismatch refused: {error}"
    return False, "a declaration that does not match its domain's contract was built anyway"


def face_bool_for_numeric_on_a_contract_row() -> tuple[bool, str]:
    """F4 -- a published boolean is refused even when the row class is the domain's own contract.

    Binding the row model to the contract class moved row construction out of the synthesized
    annotations, and pydantic widens ``True`` to ``1`` for a numeric field: the guard that made
    ``*_SHAPE_INVALID`` fire lived in the class the engine no longer built. Positive arm: a numeric
    value normalizes to that number. Counterfact arm: the same column published as ``True`` is refused
    by the declaration, with the offending column named.
    """
    spec = spec_of(CBOE, "IndexConstituents")
    contract_type = contract_model(spec.domain)
    numeric = next(column for column in spec.columns if column.kind in ("float", "int"))
    key = numeric.source_key or numeric.name

    record = synthetic_record(spec)
    record[key] = 1.5
    row = http_json.normalize_record(record, {spec.rows_pointer: [record]}, spec, contract_type)
    value = getattr(row, numeric.name)
    if type(row) is not contract_type or value != 1.5:
        return False, f"numeric arm built {type(row).__name__}.{numeric.name}={value!r}"

    record = synthetic_record(spec)
    record[key] = True
    try:
        http_json.normalize_record(record, {spec.rows_pointer: [record]}, spec, contract_type)
    except ProviderEngineError as error:
        if not str(error.code).endswith("SHAPE_INVALID"):
            return False, f"bool arm raised code={error.code}"
        if numeric.name not in list(getattr(error, "rejected", ())):
            return False, f"the refusal did not name the column: {error}"
    else:
        return False, "a boolean satisfied a declared numeric column"

    required = next(column for column in spec.columns if column.required)
    record = synthetic_record(spec)
    record.pop(required.source_key or required.name)
    try:
        http_json.normalize_record(record, {spec.rows_pointer: [record]}, spec, contract_type)
    except ProviderEngineError as error:
        if required.name not in list(getattr(error, "missing", ())):
            return False, f"the absent-column refusal did not name it: {error}"
    else:
        return False, f"a row missing the required column {required.name!r} was published"
    return True, (
        f"{key}=True -> {numeric.name} in rejected; without {required.name} -> {required.name} in "
        f"missing; {key}=1.5 -> {numeric.name}={value!r}"
    )


def face_provenance_domain_checks() -> tuple[bool, str]:
    """F5 -- the three new audit codes fire against the live registry, and correct rows stay green.

    Positive arms: the audit's own self-test reports every mutating arm fired, and every live
    declaration produces no violation code from ``domain_disagree`` over ``registered_domains()``.
    Counterfact arms: three mutations of a real declaration -- a domain nobody registered, a domain the
    registry has but never reviewed, and a column the contract does not publish -- each have to raise
    their own code from the same live data.
    """
    arms, fired, notes = run_self_test()
    if arms != fired or notes:
        return False, f"self-test arms={arms} fired={fired} notes={notes}"

    domains = registered_domains()
    specs = [spec for _, spec in engine_declared_models()]
    if not specs:
        return False, "no engine declaration exists to judge"
    for spec in specs:
        if codes(domain_disagree(spec, domains)):
            return False, f"{spec.model} is refused by its own declaration on {spec.domain!r}"

    unreviewed = sorted(domain for domain, (declared, _) in domains.items() if not declared)
    if not unreviewed:
        return False, "DOMAIN_NO_SEMANTICS has no arm: every registered domain is reviewed"

    spec = specs[0]
    mutations = (
        ("DOMAIN_UNREGISTERED", replace(spec, domain="probe_no_such_domain")),
        ("DOMAIN_NO_SEMANTICS", replace(spec, domain=unreviewed[0])),
        (
            "CONTRACT_COLUMNS_DISAGREE",
            replace(spec, columns=(*spec.columns, ColumnSpec("probe_invented", "str"))),
        ),
    )
    for expected, tampered in mutations:
        produced = codes(domain_disagree(tampered, domains))
        if expected not in produced:
            return False, f"{expected} did not fire (got {sorted(produced)})"
    return True, (
        f"self-test {fired}/{arms}; live declarations green over {len(domains)} domains; "
        f"3 mutations fired (unreviewed arm {unreviewed[0]!r})"
    )


def face_capability_census() -> tuple[bool, str]:
    """F6 -- every source's bindings reach the registry one-for-one, per source.

    The registry keys by capability, so a source whose two bindings collide on one key registers one
    capability while its module still says two. Each source is therefore counted three ways -- the
    module's ``FETCHERS`` tuple, the capabilities the call returns, and what a *fresh* registry holds
    after it -- and the per-source numbers have to agree before the whole-tree sum is read. Counterfact
    arm: the same capability cannot be registered twice, which is what stops a census from being
    inflated by a re-registration.
    """
    from opendata.data.providers.catalog import register_provider

    registry = ProviderRegistry()
    registered = register_providers(registry)
    declared = engine_declared_models()

    rows: list[tuple[str, int]] = []
    for provider in registration_order():
        module = importlib.import_module(
            f"opendata.data.providers.{provider.source}.registration"
        )
        fresh = ProviderRegistry()
        returned = register_provider(provider.source, fresh)
        held = len(fresh.capabilities())
        if len(module.FETCHERS) != len(returned) or len(returned) != held:
            return False, (
                f"{provider.source}: module={len(module.FETCHERS)} "
                f"returned={len(returned)} held={held}"
            )
        rows.append((provider.source, held))

    if sum(count for _, count in rows) != len(registered):
        return False, f"per-source {sum(count for _, count in rows)} != registered {len(registered)}"
    for source, spec in declared:
        registry.resolve_model(source, spec.model)
        domain = load_domains().get(spec.domain)
        if domain is None or not domain.semantics_declared or "query" not in domain.permissions:
            return False, f"{source}::{spec.model} declares an unservable domain {spec.domain!r}"

    duplicate = next(iter(registry._fetchers.values()))  # noqa: SLF001 - the keying is the subject
    try:
        registry.register(duplicate)
    except ValueError:
        pass
    else:
        return False, "a duplicate capability key registered silently"

    reviewed = sum(1 for spec in load_domains().values() if spec.semantics_declared)
    return True, (
        f"registered={len(registered)} = hand {len(registered) - len(declared)} + engine "
        f"{len(declared)} over {len(rows)} sources {dict(rows)}; domains={len(load_domains())} "
        f"reviewed={reviewed}; duplicate key refused"
    )


def face_rights_rows_are_load_bearing() -> tuple[bool, str]:
    """F7 -- deleting a registration row reddens exactly the sources that name it.

    ``provider-inventory.yaml`` is the baseline AC-1 routing progress is read from, and the plane's
    ``RIGHTS LINK`` rule is what makes a ``rights_rows:`` name mean something. A green baseline on
    its own cannot show that: a name nothing actually reads stays green when its row is deleted. So
    every row name the serving sources declare is removed from a copy of §1 and the shipped plane is
    re-run over the copy -- the only findings allowed are one ``RIGHTS LINK`` per declaring source.
    """
    inventory_text = (
        REPO_ROOT / "docs/proposals/openbb-migration/provider-inventory.yaml"
    ).read_text(encoding="utf-8")
    rights_text = (REPO_ROOT / "docs/data-rights-registry.md").read_text(encoding="utf-8")
    legs = plane.live_legs()
    served = {source: pairs for source, pairs in legs.items() if pairs}
    rows = {row.provider: row.fields for row in plane.parse_rows(inventory_text)}

    declared: dict[str, list[str]] = {}
    for source in sorted(served):
        for link in plane.declared_rights(rows[source].get("rights_rows", "")):
            declared.setdefault(link, []).append(source)
    naming = {source for sources in declared.values() for source in sources}
    if naming != set(served):
        return False, f"serving={len(served)} but only {len(naming)} declare a row: {sorted(naming)}"

    baseline = plane.findings(inventory_text, rights_text, served)
    if [str(finding) for finding in baseline]:
        return False, f"the shipped record is not green before any deletion: {[str(f) for f in baseline]}"

    lines = rights_text.splitlines()
    indexes = registration_rows(lines)
    numbers = sorted(number for _, number, _ in indexes)
    if numbers != list(range(1, len(numbers) + 1)):
        return False, f"§1 row numbers are not a contiguous 1..N set: {numbers}"
    for link, sources in sorted(declared.items()):
        dropped = {index for index, _, name in indexes if link in name}
        if not dropped:
            return False, f"`{link}` matches no §1 row, so its deletion arm could not differ"
        stripped = "\n".join(line for index, line in enumerate(lines) if index not in dropped)
        found = plane.findings(inventory_text, stripped, served)
        got = [(finding.kind, finding.text.split("`")[1]) for finding in found]
        want = [("RIGHTS LINK", source) for source in sorted(sources)]
        if got != want:
            return False, f"deleting `{link}` gave {got}, wanted {want}"

    return True, (
        f"serving_sources={len(served)} row_names={len(declared)} section1_rows={len(numbers)}; "
        f"each of the {len(declared)} deletions reddened only its declaring source; baseline green"
    )


FACES: tuple[tuple[str, Any], ...] = (
    ("F1 resolve_domain routes by the asked-for source's own asset class", face_resolve_domain),
    ("F2 an undeclared parameter is refused by naming every rejected key", face_rejected_parameter_names),
    ("F3 rows are the domain's contract class, and only that class is served", face_contract_row_class),
    ("F4 a boolean is refused for a numeric column on a contract row", face_bool_for_numeric_on_a_contract_row),
    ("F5 the provenance audit's domain checks arm against the live registry", face_provenance_domain_checks),
    ("F6 the capability census holds as an identity across two enumerations", face_capability_census),
    ("F7 every declared rights row is load-bearing for its serving source", face_rights_rows_are_load_bearing),
)


def main() -> int:
    """Run every face, print one line per side, and fail if any side did not land."""
    failed = 0
    for label, face in FACES:
        try:
            ok, detail = face()
        except Exception as error:  # noqa: BLE001 - a face that crashes is a face that did not land
            ok, detail = False, f"{type(error).__name__}: {error}"
        print(f"{'PASS' if ok else 'FAIL'}  {label}")
        print(f"      {detail}")
        failed += 0 if ok else 1
    print(f"FACES={len(FACES)} PASS={len(FACES) - failed} FAIL={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
