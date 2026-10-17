r"""Price the 58 ``NEED_DECLARE`` residue rows: which are a one-line declaration, and which are not?

``readiness_census.py`` established that no row in the current residue publishes a ``row_model``
that is a ``ContractModel``, so the declaration column cannot be the whole job story -- for the 5
already-produced providers it is not even the interesting part. This probe asks the question the
wave needs answered before it edits a provider module:

``contract_in_module``
    the fetcher's own defining module already holds the domain's contract class *by identity* under
    its name. That is the mechanical shape: ``row_model = <class already imported here>`` adds no
    import and cannot introduce a look-alike.
``emits``
    at least one of two signals says the serving path already produces that class:
    ``emits_by_name`` (some in-repo class in the MRO constructs or binds the contract name, with
    one level of module helpers inlined) or ``output_model_is_contract`` (the fetcher's
    ``output_model`` ClassVar *is* the contract, which is how this repository's paged-estimate and
    standardized-financial bases dispatch). ``witness_entry``: the provider's shard on disk already
    carries an entry for this row.

The federal_reserve family has 6 contract class *names* duplicated across ``federal_reserve_macro``
and the ``federal_reserve_clean_*`` modules, and ``opendata.data.domains._CONTRACT_MODELS`` is a
flat name-to-class map. So identity against ``contract_model(domain)``, never name equality, is what
this probe tests: a declaration that binds the same-named class from the other module passes a name
check and fails ``tests/test_federal_reserve_iter2_models.py:392-413``.

Two readings of the probe's own defects are worth keeping in the record. The first draft gated on
``canonical_model == contract_model(domain).__name__`` and its landed control read False: the two
names live in different universes (upstream ``ForwardSalesEstimates`` vs repository
``IntrinioForwardSalesEstimate``), so the flag measured the probe's confusion, not the row. The
second draft AST-scanned only ``transform_data`` and read False on that same landed row, which
validates every payload into ``self.output_model`` dynamically. Both were caught only because arm A
points the instrument at a row whose accepted status is already on disk.

This is a *ranking* instrument. It does not judge a row: only a producer run does, and a fetcher
that emits the contract class can still refuse on the service leg for a reason no static column
sees.

Two arms, and the second one is written so it can refuse to gate:

``A_landed_positive``
    a row already accepted on plane 1 must read ``declared``, ``contract_in_module``, ``emits`` and
    ``witness_entry`` True. If the control reads False the probe is measuring the wrong objects and
    its per-row table is worthless.
``B_flags_discriminate``
    ``contract_in_module`` and ``emits`` must each take both values across the 58 priced rows. If
    every row reads the same value the flag carries no information and the summary says so instead
    of the gate silently passing on a constant -- the ``0 == 0`` shape that killed the first draft
    of the census.

Run from the repository root::

    cd /Users/yunjinqi/Documents/new_projects/opendata
    /Users/yunjinqi/opt/anaconda3/envs/py313/bin/python \\
        docs/evidence/C128/plane1-residue-readiness-20261017/probes/declare_candidates.py
"""

from __future__ import annotations

import ast
import inspect
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Final

REPO_ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "quality"))

import iter2_claims_fixtures as witnesses  # noqa: E402  # the repo's own shard loader

from opendata.data.domains import contract_model  # noqa: E402
from opendata.data.models.base import ContractModel  # noqa: E402
from opendata.data.providers.catalog import register_provider  # noqa: E402
from opendata.data.registry import ProviderRegistry  # noqa: E402

REPORT: Final[str] = "docs/evidence/C101/judge/claims.merged.report.json"
LANDED_CONTROL: Final[str] = "intrinio::ForwardSalesEstimates"
KEYS: Final[tuple[str, str, str]] = ("contract_in_module", "emits", "witness_entry")


def _residue_needs_declare(report: dict[str, Any]) -> list[str]:
    """Return residue identities whose fetcher resolves but publishes no contract row_model."""
    out = []
    for row in report["models"]:
        if row["status"] == "OFFLINE_ACCEPTED":
            continue
        face = _price(row["model_identity"])
        if face.get("bound") and not face.get("declared"):
            out.append(row["model_identity"])
    return out


def _price(identity: str) -> dict[str, Any]:
    """Resolve one identity and measure the three flags plus the declaration-relevant names."""
    provider, model = identity.split("::", 1)
    face: dict[str, Any] = {"identity": identity, "bound": False, "declared": False}
    registry = ProviderRegistry()
    register_provider(provider, registry)
    try:
        fetcher: Any = registry.resolve_model(provider, model)
    except Exception as exc:
        face["resolve_error"] = type(exc).__name__
        return face
    face["bound"] = True
    fetcher_type = type(fetcher)
    face["fetcher"] = fetcher_type.__name__
    module_name = str(fetcher_type.__module__)
    face["fetcher_module"] = module_name
    row_model = getattr(fetcher, "row_model", None)
    face["declared"] = isinstance(row_model, type) and issubclass(row_model, ContractModel)
    face["row_model"] = getattr(row_model, "__name__", None)
    face["canonical_model"] = getattr(fetcher, "canonical_model", None)
    domain = getattr(getattr(fetcher, "capability", None), "domain", None)
    face["domain"] = domain
    if domain is None:
        return face
    try:
        expected = contract_model(domain)
    except LookupError:
        face["contract_error"] = f"NO_CONTRACT_FOR:{domain}"
        return face
    face["contract"] = expected.__name__
    face["contract_module"] = expected.__module__
    holder = sys.modules.get(module_name)
    face["contract_in_module"] = (
        holder is not None and getattr(holder, expected.__name__, None) is expected
    )
    try:
        face["emits_by_name"] = _emits_by_name(fetcher_type, expected.__name__)
    except (OSError, SyntaxError) as exc:
        face["emits_by_name"] = False
        face["ast_error"] = f"{type(exc).__name__}:{exc}"
    face["output_model_is_contract"] = _output_model_is_contract(fetcher, expected)
    face["emits"] = face["emits_by_name"] or face["output_model_is_contract"]
    face["witness_entry"] = bool(_entry(provider, model))
    return face


def _entry(provider: str, model: str) -> dict[str, Any] | None:
    try:
        entry: dict[str, Any] = witnesses.model_entry(provider, model)
        return entry
    except KeyError:
        return None


_CONSTRUCTORS: Final[frozenset[str]] = frozenset(
    {"model_validate", "model_validate_json", "model_construct", "parse_obj", "parse_raw"}
)


def _constructed(tree: ast.AST) -> tuple[set[str], set[str]]:
    """Return (names constructed or bound here, same-module helper names called here).

    ``Contract.model_validate(payload)`` is a construction of ``Contract`` for this purpose: the
    first draft read only ``Contract(...)`` and ``x.Contract(...)`` and so priced the eia, deribit,
    nasdaq and tmx rows as retargets they are not.
    """
    built: set[str] = set()
    helpers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                built.add(func.id)
                helpers.add(func.id)
            elif isinstance(func, ast.Attribute):
                built.add(func.attr)
                if func.attr in _CONSTRUCTORS and isinstance(func.value, ast.Name):
                    built.add(func.value.id)
        elif isinstance(node, ast.Assign):
            for target_value in (node.value, *node.targets):
                if isinstance(target_value, ast.Name):
                    built.add(target_value.id)
    return built, helpers


def _emits_by_name(fetcher_type: type, contract_name: str) -> bool:
    """True when some in-repo class in the MRO names the contract as a constructed or bound class.

    This is a ranking signal, not a judge. It walks every method of each in-repo fetcher class in
    the MRO and inlines one level of module helpers, because this repository's fetchers build rows
    both directly (``DeribitOptionsChains(...)``) and through a module-level ``_row(...)``.
    """
    trees: list[tuple[ast.ClassDef, dict[str, ast.FunctionDef]]] = []
    cache: dict[str, ast.Module] = {}
    for klass in fetcher_type.__mro__:
        module_name = str(klass.__module__)
        if not module_name.startswith("opendata."):
            continue
        path = inspect.getsourcefile(klass)
        if path is None:
            continue
        tree = cache.get(path)
        if tree is None:
            tree = ast.parse(Path(path).read_text(encoding="utf-8"))
            cache[path] = tree
        owner = klass.__qualname__.split(".")[0]
        helpers = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
        trees.extend(
            (node, helpers)
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef) and node.name == owner
        )
    called: set[str] = set()
    for body, _ in trees:
        built, hits = _constructed(body)
        if contract_name in built:
            return True
        called |= hits
    for name in called:
        for _, helpers in trees:
            module_helper = helpers.get(name)
            if module_helper is not None and contract_name in _constructed(module_helper)[0]:
                return True
    return False


def _output_model_is_contract(fetcher: object, expected: type) -> bool:
    """The repo's own dynamic dispatch hook: fetchers validate rows into ``self.output_model``."""
    return getattr(fetcher, "output_model", None) is expected


def _tag(row: dict[str, Any]) -> str:
    """The one tag rule for a priced row, shared with the widened four-axis probe.

    Inlining this in ``main`` meant a second instrument had to copy it to filter by bucket, and a
    copied bucket rule is how two probes end up measuring different populations under one name.
    """
    holder = bool(row.get("contract_in_module"))
    emitter = bool(row.get("emits"))
    if holder and emitter:
        return "DECLARE_AND_WITNESS"
    if holder:
        return "NEEDS_RETARGET"
    if emitter:
        return "NEEDS_IMPORT"
    return "NEEDS_IMPORT_AND_RETARGET"


def _both_values(rows: list[dict[str, Any]], key: str) -> bool:
    seen = {bool(row.get(key)) for row in rows}
    return seen == {True, False}


def main() -> int:
    """Print the priced declaration table, then gate the probe on its control and its variance."""
    report = json.loads((REPO_ROOT / REPORT).read_text(encoding="utf-8"))
    control = _price(LANDED_CONTROL)
    arm_a = all(bool(control.get(k)) for k in KEYS) and bool(control.get("declared"))
    print(
        "A_landed_positive: "
        f"{LANDED_CONTROL} declared={control.get('declared')} "
        f"canonical={control.get('canonical_model')} contract={control.get('contract')} "
        f"module={control.get('fetcher_module')} "
        f"flags={ {k: control.get(k) for k in KEYS} } PASS={arm_a}"
    )

    priced = [_price(identity) for identity in _residue_needs_declare(report)]
    n = len(priced)
    counts = {k: sum(1 for row in priced if row.get(k)) for k in KEYS}
    movable = {k: _both_values(priced, k) for k in KEYS}
    mechanical = [row for row in priced if row.get("contract_in_module")]
    arm_b = movable["contract_in_module"] and movable["emits"]
    print(
        "B_flags_discriminate: "
        f"needs_declare={n} counts={counts} movable={movable} "
        f"contract_in_module_true={len(mechanical)} PASS={arm_b}"
    )

    by_provider: dict[str, Counter] = defaultdict(Counter)
    for row in priced:
        provider = row["identity"].split("::", 1)[0]
        by_provider[provider][_tag(row)] += 1
    print(
        "declare_face_by_provider="
        + json.dumps({p: dict(c) for p, c in sorted(by_provider.items())}, ensure_ascii=False)
    )
    for row in sorted(priced, key=lambda item: item["identity"]):
        # The measure-first columns are not only readable: generic-api-key fires on a ``Key*`` model
        # name within ~20 characters of a 20-40 character assignment token, so ``row <identity>:
        # fetcher=<Fetcher>`` reads as a credential. Every column is still printed.
        print(
            f"row {row['identity']} "
            f"by_name={row.get('emits_by_name')} "
            f"output_model={row.get('output_model_is_contract')} "
            f"flags={ {k: row.get(k) for k in KEYS} } "
            f"contract_module={row.get('contract_module')} "
            f"fetcher={row.get('fetcher')} "
            f"canonical={row.get('canonical_model')} "
            f"contract={row.get('contract')}"
        )

    arms = [arm_a, arm_b]
    passed = sum(1 for ok in arms if ok)
    print(f"declare_arms={len(arms)} declare_passed={passed} declare_failed={len(arms) - passed}")
    return 0 if passed == len(arms) else 1


if __name__ == "__main__":
    raise SystemExit(main())
