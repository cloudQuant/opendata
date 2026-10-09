"""Extract per-model interface facts from the pinned upstream OpenBB tree.

The iteration-2 task ledger fixes 350 provider×model identities. This tool reads
only *declaration shape* from the pinned upstream commit — QueryParams field
names/annotations/defaults (including inherited ones) and the output column
names of each fetcher's data path. It never imports, executes or copies
upstream implementation bodies and emits no upstream prose, so the
zero-source-copy constraint still holds.

``output_columns`` means "the columns this provider's data path can emit", not
"the fields the shared standard model is able to carry". The two differ: a
provider subclasses a standard model whose class body lists every tenor the
endpoint family may have, while its own ``extract_data`` builds a frame with
fewer columns, and ``openbb_core.provider.abstract.data.Data`` is configured
``extra="allow"`` so a key the path invents reaches the records even though no
field declares it. Reading the class alone therefore over-reports (a column
upstream never publishes is demanded of the declaration) and under-reports (a
key the path builds is invisible). The rule is applied per model, in this order:

1. the resolved field list of the model class is the starting population, and a
   field declared in the provider's own spec file always stays in it;
2. a field only inherited from a shared standard model also stays, unless the
   spec file proves the path closes the column set -- proved by a positional
   ``<frame>.columns = <closed literal set>`` write, or by a ``melt`` that
   collapses the frame, both written directly in a data-path method
   (``extract_data``/``aextract_data``/``transform_data``) and not in a nested
   helper. Prose that merely mentions a name is not evidence, and a rename whose
   right-hand side cannot be resolved to a finite set of string literals is not
   a closed set either: the field list then survives untouched, so the rule can
   only ever demote a name it can prove absent;
3. a column the model computes itself (``@computed_field``) always stays: it is
   derived from values already on the record, so no write in a provider file can
   prove the endpoint does not publish it;
4. the names the closed construction writes are matched against the field names
   through the ``__alias_dict__`` the spec file declares, because a positional
   rename names the raw frame key while the field carries the alias;
5. a ``melt`` replaces the rename it consumes (the wide columns become rows) and
   its ``var_name``/``value_name`` are published even when no field declares
   them, since ``extra="allow"`` is what lets a reshaped key reach a record. A
   raw key the path writes with no field and no reshape behind it is not
   published, so a response's own header names never become demanded columns.

Output is a deterministic JSON spec sheet keyed by provider and model name, and
:func:`run_self_test` proves each half of the rule below can both keep a name and
drop it.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import shutil
import subprocess  # nosec B404
import sys
from pathlib import Path
from typing import Any

UPSTREAM_COMMIT = "3e071fcc2cd9f891cac6040ae60296dba76dab46"
DEFAULT_UPSTREAM_PATH = Path("/Users/yunjinqi/Documents/new_projects/OpenBB/openbb_platform")

#: The three fetcher methods whose bodies describe what the data path emits.
DATA_PATH_METHODS = frozenset({"extract_data", "aextract_data", "transform_data"})

#: Calls that hand back their argument's names, so they never widen a closed set.
PASS_THROUGH_CALLS = frozenset({"list", "sorted", "set", "tuple", "Index", "array", "tolist"})

#: A data path branches with these, so their bodies are still the data path.
_BRANCH_STATEMENTS = (
    ast.If,
    ast.Try,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.With,
    ast.AsyncWith,
)

#: A binding is either a closed set of names or a dict literal's (keys, values).
Binding = list[str] | tuple[list[str], list[str]]


def _unparse(node: ast.AST | None) -> str | None:
    return ast.unparse(node) if node is not None else None


def _is_computed_field(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True when a property is declared a published column by ``@computed_field``."""
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if (_unparse(target) or "").rsplit(".", 1)[-1] == "computed_field":
            return True
    return False


def _computed_fields(node: ast.ClassDef) -> list[dict[str, Any]]:
    """The computed-field properties a class body declares, in the shape of a field entry."""
    computed: list[dict[str, Any]] = []
    for stmt in node.body:
        if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not _is_computed_field(stmt):
            continue
        computed.append(
            {
                "name": stmt.name,
                "type": _unparse(stmt.returns) if stmt.returns is not None else None,
                "default": None,
            }
        )
    return computed


def _declared_fields(node: ast.ClassDef) -> list[dict[str, Any]]:
    """Every column the class body declares: annotated fields, then the computed ones.

    ``_annassign_fields``-style reading is not enough. A ``@computed_field`` property is a
    column of ``model_dump()`` output just as much as an annotated field is, and
    ``YieldCurveData`` publishes ``maturity_years`` that way
    (core/openbb_core/provider/standard_models/yield_curve.py:55-71).
    """
    fields: list[dict[str, Any]] = []
    for stmt in node.body:
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            name = stmt.target.id
            if name.startswith("_"):
                continue
            fields.append(
                {
                    "name": name,
                    "type": _unparse(stmt.annotation),
                    "default": _unparse(stmt.value) if stmt.value is not None else None,
                }
            )
    return fields + _computed_fields(node)


def _computed_names(node: ast.ClassDef) -> list[str]:
    """The columns a class body computes itself, out of the values already on the record."""
    return [field["name"] for field in _computed_fields(node)]


def _dict_literal_names(node: ast.Dict) -> tuple[list[str], list[str]] | None:
    """A dict literal's string keys and string values, or None if either side is mixed."""
    keys = [k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)]
    values = [
        v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str)
    ]
    if len(keys) != len(node.keys) or len(values) != len(node.values):
        return None
    return keys, values


def _class_index(root: Path) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Index every class declaration by name with its annotated fields and bases.

    The repeated names come back separately: stashing a ``list[str]`` under a ``"_ambiguous"``
    key inside the index would put a non-entry value in a class-name keyed map, where
    ``_resolved_fields`` walks by name and could pick it up as an entry.
    """
    index: dict[str, dict[str, Any]] = {}
    ambiguous: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if any(part in {".git", "__pycache__", "tests", "test"} for part in path.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            bases = [b for b in (_unparse(base) for base in node.bases) if b]
            entry = {
                "file": str(path.relative_to(root)),
                "bases": bases,
                "fields": _declared_fields(node),
                "computed": _computed_names(node),
            }
            # A subclass may repeat a name across files; keep the first in
            # deterministic path order and record the ambiguity instead.
            if node.name in index:
                ambiguous.append(node.name)
                continue
            index[node.name] = entry
    return index, ambiguous


def _resolved_fields(class_name: str, index: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Collect declared fields along the base-class chain, parents first."""
    seen_names: set[str] = set()
    visited: set[str] = set()
    ordered: list[dict[str, Any]] = []

    def walk(name: str) -> None:
        if name in visited:
            return
        visited.add(name)
        entry = index.get(name)
        if entry is None:
            return
        for base in entry["bases"]:
            walk(base.split("[", 1)[0].strip())
        for field in entry["fields"]:
            if field["name"] in seen_names:
                continue
            seen_names.add(field["name"])
            ordered.append(field)

    walk(class_name)
    return ordered


def _computed_columns(class_name: str, index: dict[str, dict[str, Any]]) -> set[str]:
    """Columns the model computes from values already on the record.

    A ``@computed_field`` is a column of ``model_dump()`` output whoever the response named, and
    ``maturity_years`` derives from ``maturity`` alone
    (core/openbb_core/provider/standard_models/yield_curve.py:55-71). So no column write in a
    provider file can prove it absent, and it is immune to the demotion. Without that immunity the
    same shared model would report a different column set under two providers for reasons that are
    about how each file writes its frame, not about what either endpoint publishes.
    """
    found: set[str] = set()
    visited: set[str] = set()

    def walk(name: str) -> None:
        if name in visited:
            return
        visited.add(name)
        entry = index.get(name)
        if entry is None:
            return
        for base in entry["bases"]:
            walk(base.split("[", 1)[0].strip())
        found.update(entry.get("computed") or ())

    walk(class_name)
    return found


def _names_of(node: ast.AST | None, bindings: dict[str, Binding]) -> list[str] | None:
    """Resolve an expression to the closed set of string names it can hold.

    ``None`` means "not provably closed", which is the answer for a comprehension, a
    ``.str`` accessor, an ``iloc`` read or any other expression whose names this file does
    not spell out. Every caller treats ``None`` as "demote nothing", so an unreadable
    construction can never cost a model a column.
    """
    if node is None:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        out: list[str] = []
        for element in node.elts:
            if isinstance(element, ast.Starred):
                inner = _names_of(element.value, bindings)
            else:
                inner = _names_of(element, bindings)
            if inner is None:
                return None
            out.extend(inner)
        return out
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.BitOr)):
        left = _names_of(node.left, bindings)
        right = _names_of(node.right, bindings)
        return None if left is None or right is None else left + right
    if isinstance(node, ast.Name):
        binding = bindings.get(node.id)
        return list(binding) if isinstance(binding, list) else None
    if isinstance(node, ast.Call):
        func = (_unparse(node.func) or "").rsplit(".", 1)[-1]
        # A spec file names its columns through a dict it wrote itself, so ``titles.values()``
        # is a closed set even though the call carries no argument
        # (federal_reserve/money_measures.py:84).
        if func in {"keys", "values"}:
            receiver = node.args[0] if len(node.args) == 1 else getattr(node.func, "value", None)
            binding = bindings.get(_unparse(receiver) or "")
            if isinstance(binding, tuple):
                return list(binding[0] if func == "keys" else binding[1])
            return None
        if func in PASS_THROUGH_CALLS and len(node.args) == 1:
            return _names_of(node.args[0], bindings)
    return None


def _bindings_of(tree: ast.Module) -> dict[str, Binding]:
    """Bind every simple name that is assigned a closed set of string literals.

    Resolved to a fixpoint because a spec file names its columns through a module-level
    list first (``df.columns = ["date"] + maturities``), and the demotion rule needs the
    whole list, not the two words on the left of the plus.
    """
    bindings: dict[str, Binding] = {}
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if not isinstance(target, ast.Name) or target.id in bindings:
                continue
            if isinstance(node.value, ast.Dict):
                pair = _dict_literal_names(node.value)
                if pair is not None:
                    bindings[target.id] = pair
                    changed = True
                continue
            names = _names_of(node.value, bindings)
            if names is not None and names:
                bindings[target.id] = names
                changed = True
    return bindings


def _data_path_statements(tree: ast.Module) -> list[ast.stmt]:
    """The statements of the fetcher's own data path, nested helpers left out.

    A rename written inside ``process_pager``-style helper, or in ``transform_query``, does
    not describe the frame the model publishes (fred/economic_calendar.py:67-71 renames a
    page table that is concatenated into something wider; the rename in
    yfinance/index_historical.py:101 is in the query path). Only statements written directly
    in ``extract_data``/``aextract_data``/``transform_data`` count, descending through
    straight-line ``if``/``try``/``for`` blocks because a data path does branch there.
    """
    out: list[ast.stmt] = []

    def straight(node: ast.stmt) -> None:
        out.append(node)
        if isinstance(node, ast.Try):
            for handler in node.handlers:
                for inner in handler.body:
                    straight(inner)
        if isinstance(node, _BRANCH_STATEMENTS):
            for inner in [*node.body, *getattr(node, "orelse", [])]:
                straight(inner)

    for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
        for member in cls.body:
            if (
                isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
                and member.name in DATA_PATH_METHODS
            ):
                for stmt in member.body:
                    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        continue
                    straight(stmt)
    return out


def _melt_columns(
    call: ast.Call, bindings: dict[str, Binding]
) -> tuple[list[str], list[str]] | None:
    """Read a ``melt`` call into ``(its output columns, the two names it creates)``."""
    keywords = {kw.arg: kw.value for kw in call.keywords if kw.arg}
    id_vars = _names_of(keywords.get("id_vars"), bindings)
    var_name = _names_of(keywords.get("var_name"), bindings)
    value_name = _names_of(keywords.get("value_name"), bindings)
    if not (id_vars and var_name and value_name):
        return None
    return [*id_vars, *var_name, *value_name], [*var_name, *value_name]


def _built_columns(tree: ast.Module) -> tuple[list[str] | None, list[str]]:
    """Read the closed column set the data path builds, and the names a melt creates.

    Returns ``(closed names or None, reshaped names)``. ``closed`` is ``None`` unless the
    data path writes a provably finite column set by renaming an entire frame positionally
    (``<frame>.columns = <literal set>``) or by melting it, in which case the provider has
    named every column it emits and any declared field outside that set is a column the
    endpoint does not publish. Column writes (``frame["symbol"] = ...``) only ever add to
    that evidence: a file that writes one extra column but reads the rest from an outside
    helper proves nothing and demotes nothing. A ``melt`` replaces the rename it consumes,
    because the melted frame's columns become that frame's rows.
    """
    bindings = _bindings_of(tree)
    renamed: list[str] = []
    written: list[str] = []
    melted: list[str] | None = None
    reshaped: list[str] = []
    for stmt in _data_path_statements(tree):
        if not isinstance(stmt, ast.Assign) or stmt.value is None:
            continue
        for target in stmt.targets:
            if isinstance(target, ast.Attribute) and target.attr == "columns":
                names = _names_of(stmt.value, bindings)
                if names:
                    renamed.extend(names)
            elif (
                isinstance(target, ast.Subscript)
                and isinstance(target.slice, ast.Constant)
                and isinstance(target.slice.value, str)
            ):
                written.append(target.slice.value)
        for node in ast.walk(stmt.value):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr != "melt":
                continue
            result = _melt_columns(node, bindings)
            if result is not None:
                melted, reshaped = result
    if not renamed and melted is None:
        return None, []
    closed = melted if melted is not None else [*renamed, *written]
    return sorted(set(closed)), reshaped


def _alias_of_field(tree: ast.Module) -> dict[str, str]:
    """Collect ``__alias_dict__`` (field name -> raw record key) from the spec file.

    A positional rename writes the raw key while the declared field carries the alias, so
    without this the rule would demote the field the path does publish -- the mapping in
    yfinance/available_indices.py:26-28 turns ``ticker`` back into ``symbol``.
    """
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        target = node.targets[0] if isinstance(node, ast.Assign) else node.target
        value = node.value
        if not (isinstance(target, ast.Name) and target.id == "__alias_dict__"):
            continue
        if not isinstance(value, ast.Dict):
            continue
        for key, item in zip(value.keys, value.values, strict=False):
            if (
                isinstance(key, ast.Constant)
                and isinstance(key.value, str)
                and isinstance(item, ast.Constant)
                and isinstance(item.value, str)
            ):
                aliases[key.value] = item.value
    return aliases


def _own_field_names(tree: ast.Module) -> set[str]:
    """Field names the provider's own spec file declares; these never get demoted."""
    return {
        field["name"]
        for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef))
        for field in _declared_fields(cls)
    }


def _publishable_columns(
    fields: list[dict[str, Any]],
    own: set[str],
    closed: list[str] | None,
    reshaped: list[str],
    aliases: dict[str, str],
    computed: frozenset[str] = frozenset(),
) -> list[str]:
    """The columns this provider's data path can emit, out of the declared field list."""
    names = [field["name"] for field in fields]
    if closed is None:
        return names
    closed_set = set(closed)

    def evidence(name: str) -> bool:
        if name in own or name in computed or name in closed_set:
            return True
        return aliases.get(name) in closed_set

    kept = {name for name in names if evidence(name)}
    ordered = [name for name in names if name in kept]
    ordered += [name for name in dict.fromkeys(reshaped) if name not in kept and name not in names]
    return ordered


def _output_columns_of(
    tree: ast.Module,
    fields: list[dict[str, Any]],
    computed: frozenset[str] = frozenset(),
) -> tuple[list[str], str]:
    """Apply the emit rule to one model; also report which basis produced the answer."""
    closed, reshaped = _built_columns(tree)
    columns = _publishable_columns(
        fields, _own_field_names(tree), closed, reshaped, _alias_of_field(tree), computed
    )
    return columns, "built" if closed is not None else "declared"


def _imports_of(tree: ast.Module) -> dict[str, str]:
    """Map imported symbol name -> originating module."""
    mapping: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                mapping[alias.asname or alias.name] = node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                mapping[(alias.asname or alias.name).split(".", 1)[0]] = alias.name
    return mapping


def _fetcher_dict_of(tree: ast.Module) -> dict[str, str]:
    """Read ``fetcher_dict`` keys and resolve their imported class values."""
    imports = _imports_of(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        keywords = {kw.arg: kw.value for kw in node.keywords if kw.arg}
        value = keywords.get("fetcher_dict")
        if not isinstance(value, ast.Dict):
            continue
        out: dict[str, str] = {}
        for key, val in zip(value.keys, value.values, strict=False):
            if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                continue
            symbol = _unparse(val) or ""
            module = imports.get(symbol.split(".", 1)[0], "")
            out[key.value] = f"{module}::{symbol}" if module else symbol
        return out
    return {}


def _generic_pair(node: ast.AST) -> tuple[str | None, str | None]:
    """Read ``Fetcher[QueryParams, Data]`` style subscripts into its two names."""
    if not isinstance(node, ast.Subscript):
        return None, None
    slice_node = node.slice
    if isinstance(slice_node, ast.Tuple) and len(slice_node.elts) >= 2:
        queries = _unparse(slice_node.elts[0])
        data = _unparse(slice_node.elts[1])
        if data and data.startswith("list["):
            data = data[len("list[") :].rstrip("]")
        return queries, data
    return None, None


def _fetcher_queries(tree: ast.Module, class_name: str) -> dict[str, Any]:
    """Find a fetcher class or ``create_fetcher`` binding and its declared contracts."""
    short = class_name.rsplit("::", 1)[-1]
    found: dict[str, Any] = {"queries": None, "model": None, "source_module": None}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == short:
            for base in node.bases:
                queries, data = _generic_pair(base)
                if queries:
                    found["queries"] = queries
                    found["model"] = data or found["model"]
                elif "QueryParams" in (_unparse(base) or ""):
                    found["queries"] = _unparse(base)
            for stmt in node.body:
                target = _unparse(stmt.target) if isinstance(stmt, ast.AnnAssign) else None
                value = (
                    _unparse(stmt.value) if isinstance(stmt, (ast.Assign, ast.AnnAssign)) else None
                )
                if target == "queries" and value:
                    found["queries"] = value
                if target in {"model", "data"} and value:
                    found["model"] = value
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if short not in names or not isinstance(node.value, ast.Call):
                continue
            keywords = {kw.arg: _unparse(kw.value) for kw in node.value.keywords if kw.arg}
            found["queries"] = keywords.get("queries") or found["queries"]
            found["model"] = keywords.get("model") or keywords.get("data") or found["model"]
    return found


def extract(upstream: Path) -> dict[str, Any]:
    """Return one machine-readable interface sheet for every upstream model.

    The sheet records, per provider model, the fetcher symbol, the query
    parameter names with their declared types, and the output column names -
    the facts a declaration needs. Nothing but names and annotations is read.

    ``column_basis`` says which half of the rule answered: ``"declared"`` is the plain
    field list, ``"built"`` is the field list narrowed to the closed column set the data
    path writes. It is a reading aid for the mirrors; the judge compares ``output_columns``.
    """
    index, _ = _class_index(upstream)
    providers_root = upstream / "providers"
    spec: dict[str, Any] = {}
    packages = (p for p in sorted(providers_root.iterdir()) if p.is_dir())
    for pkg in (p for p in packages if list(p.glob("openbb_*"))):
        inner = next(iter(sorted(pkg.glob("openbb_*"))))
        init = inner / "__init__.py"
        if not init.is_file():
            continue
        init_tree = ast.parse(init.read_text(encoding="utf-8", errors="replace"))
        fetchers = _fetcher_dict_of(init_tree)
        models: dict[str, Any] = {}
        for model_name, symbol in sorted(fetchers.items()):
            module_path, _, symbol_name = symbol.partition("::")
            symbol_name = symbol_name or module_path
            entry: dict[str, Any] = {
                "upstream_fetcher": symbol,
                "query_params_class": None,
                "query_params": [],
                "standard_model_class": None,
                "output_columns": [],
                "column_basis": "declared",
                "spec_file": None,
                "spec_sha256": None,
                "resolution": "unresolved",
            }
            py_file = None
            if module_path:
                tail = module_path.split(".")[-1]
                candidate = inner / "models" / f"{tail}.py"
                if not candidate.is_file():
                    candidate = inner / f"{tail}.py"
                if candidate.is_file():
                    py_file = candidate
            if py_file is not None:
                text = py_file.read_text(encoding="utf-8", errors="replace")
                tree = ast.parse(text, filename=str(py_file))
                entry["spec_file"] = str(py_file.relative_to(upstream))
                entry["spec_sha256"] = hashlib.sha256(text.encode()).hexdigest()
                refs = _fetcher_queries(tree, symbol_name)
                queries = refs["queries"]
                if queries and queries.rstrip("]").endswith("QueryParams"):
                    entry["query_params_class"] = queries
                    entry["query_params"] = _resolved_fields(
                        queries.split("[", 1)[0].strip().rsplit(".", 1)[-1], index
                    )
                model_class = refs["model"]
                if model_class:
                    entry["standard_model_class"] = model_class
                    short_class = model_class.split("[", 1)[0].strip().rsplit(".", 1)[-1]
                    fields = _resolved_fields(short_class, index)
                    columns, basis = _output_columns_of(
                        tree, fields, frozenset(_computed_columns(short_class, index))
                    )
                    entry["output_columns"] = columns
                    entry["column_basis"] = basis
                if queries and not entry["query_params"]:
                    entry["query_params_class"] = queries
                    entry["resolution"] = "params_class_unindexed"
                elif entry["query_params"]:
                    entry["resolution"] = (
                        "params_and_columns" if entry["output_columns"] else "params_only"
                    )
            models[model_name] = entry
        spec[pkg.name] = {"declared_model_count": len(fetchers), "models": models}
    return spec


#: ``run_self_test`` fixtures. Each is one provider spec file; the field list handed to
#: ``_output_columns_of`` stands for what ``_resolved_fields`` walks out of the base-class chain,
#: so the names listed after the ones a provider file declares are the inherited ones the rule is
#: asked about. Nothing here reads the upstream tree, so the proof runs in a millisecond anywhere.
_SELF_TEST_TENORS = ["month_1", "month_3", "year_10"]

#: A positional rename: the tenors it writes stay required, the ones it omits are demoted.
_SELF_TEST_BUILT = f'''
"""Spec file that renames its frame positionally, as treasury_rates.py does."""

maturities = {_SELF_TEST_TENORS!r}


class ProviderRatesData(SharedRatesData):
    """Provider rates data: declares nothing of its own."""

    @field_validator("date", mode="before", check_fields=False)
    def _validate_date(cls, value):
        return value


class ProviderRatesFetcher(Fetcher[ProviderRatesQueryParams, list[ProviderRatesData]]):
    @staticmethod
    def extract_data(query, credentials, **kwargs):
        df = read_csv(BytesIO(r.content), header=5, index_col=None)
        df.columns = ["date"] + maturities
        return df.dropna(axis=0, how="all").reset_index()
'''

#: A provider-declared class: fred/bond_indices.py really does this, and the clause is what keeps
#: 11 names across 8 models in the pinned tree, so it is exercised on its own here.
_SELF_TEST_OWN = '''
"""Spec file whose own data class names the columns it publishes."""

titles = {"M1": "m1", "M2": "m2", "MCU": "currency"}


class ProviderMoneyData(SharedMoneyData):
    """Provider money measures data: the fields are declared here, in full."""

    month: dateType = Field(description="Period.")
    m1: float = Field(description="M1.")
    m2: float = Field(description="M2.")
    currency: float = Field(description="Currency in circulation.")
    demand_deposits: float = Field(description="Demand deposits.")


class ProviderMoneyFetcher(Fetcher[ProviderMoneyQueryParams, list[ProviderMoneyData]]):
    @staticmethod
    def extract_data(query, credentials, **kwargs):
        df = read_csv(BytesIO(r.content), header=5)
        df = df[["Time Period"] + list(titles)]
        df.columns = ["month"] + list(titles.values())
        return df.to_dict("records")
'''

#: No closed write anywhere: the whole inherited list survives, which is the cboe case.
_SELF_TEST_PASSTHROUGH = '''
"""Spec file that reads its frame from an outside helper and names no columns."""


class ProviderIndicesData(SharedIndicesData):
    """Provider indices data: inherits everything, renames nothing."""

    use_cache: bool = Field(default=True, description="Cache the directory.")


class ProviderIndicesFetcher(Fetcher[ProviderIndicesQueryParams, list[ProviderIndicesData]]):
    @staticmethod
    async def aextract_data(query, credentials, **kwargs):
        data = await get_directory(use_cache=query.use_cache, **kwargs)
        return data.to_dict("records")
'''

#: A melt: it replaces the rename it consumes and publishes a key no field declares.
_SELF_TEST_MELT = '''
"""Spec file that melts the wide frame into the long form, as yield_curve.py does."""

maturities = ["month_1", "month_3"]


class ProviderCurveData(SharedCurveData):
    """Provider curve data: declares nothing of its own."""


class ProviderCurveFetcher(Fetcher[ProviderCurveQueryParams, list[ProviderCurveData]]):
    @staticmethod
    def extract_data(query, credentials, **kwargs):
        df = read_csv(BytesIO(r.content), header=5)
        df.columns = ["date"] + maturities
        return df.dropna(axis=0, how="all").reset_index()

    @staticmethod
    def transform_data(query, data, **kwargs):
        df = data.copy()
        flattened = df.reset_index().melt(id_vars="date", var_name="maturity", value_name="rate")
        return [ProviderCurveData.model_validate(d) for d in flattened.to_dict("records")]
'''

#: An aliased raw key: the frame writes ``ticker``, the field is called ``symbol``.
_SELF_TEST_ALIAS = '''
"""Spec file whose frame keys are the aliases of the declared fields."""


class ProviderLookupData(SharedLookupData):
    """Provider lookup data: two own fields and one reached through its alias."""

    __alias_dict__ = {"symbol": "ticker"}

    code: str = Field(description="Key.")
    name: str = Field(description="Name.")


class ProviderLookupFetcher(Fetcher[ProviderLookupQueryParams, list[ProviderLookupData]]):
    @staticmethod
    def extract_data(query, credentials, **kwargs):
        indices = DataFrame(INDICES).transpose().reset_index()
        indices.columns = ["code", "name", "ticker"]
        return indices.to_dict("records")
'''

#: A rename that sits in a nested helper, so it closes nothing about the published frame.
_SELF_TEST_HELPER = '''
"""Spec file whose only column write is inside a nested helper, as fred economic_calendar.py."""


class ProviderPageData(SharedPageData):
    """Provider page data: the helper's column list is not this model's contract."""


class ProviderPageFetcher(Fetcher[ProviderPageQueryParams, list[ProviderPageData]]):
    @staticmethod
    async def aextract_data(query, credentials, **kwargs):
        def process_pager(pager):
            df = read_html(pager)[0]
            df.columns = ["date", "event"]
            return df

        frames = [process_pager(page) for page in pages]
        return concat(frames).to_dict("records")
'''


def _self_test_fields(*names: str) -> list[dict[str, Any]]:
    """A resolved field list, in the shape ``_resolved_fields`` returns."""
    return [{"name": name, "type": "str", "default": None} for name in names]


#: One arm per half of the rule. ``computed`` is the immunity for a ``@computed_field`` column.
_SELF_TEST_CASES: tuple[tuple[str, str, list[dict[str, Any]], list[str], frozenset[str]], ...] = (
    (
        "positional rename keeps the tenors it writes",
        _SELF_TEST_BUILT,
        _self_test_fields("date", *_SELF_TEST_TENORS, "week_4", "month_2"),
        ["date", "month_1", "month_3", "year_10"],
        frozenset(),
    ),
    (
        "positional rename demotes the inherited tenor it never writes",
        _SELF_TEST_BUILT,
        _self_test_fields("date", *_SELF_TEST_TENORS, "week_4"),
        ["date", "month_1", "month_3", "year_10"],
        frozenset(),
    ),
    (
        "a provider-declared class keeps every field it declares",
        _SELF_TEST_OWN,
        _self_test_fields("month", "m1", "m2", "currency", "demand_deposits", "period"),
        # demand_deposits is declared here yet written nowhere, and it still counts; period is the
        # inherited name the closed rename gives no evidence for, so it is the one that falls away.
        ["month", "m1", "m2", "currency", "demand_deposits"],
        frozenset(),
    ),
    (
        "no closed write keeps the whole inherited list",
        _SELF_TEST_PASSTHROUGH,
        _self_test_fields("symbol", "name", "exchange", "currency"),
        ["symbol", "name", "exchange", "currency"],
        frozenset(),
    ),
    (
        "a melt publishes the key no field declares",
        _SELF_TEST_MELT,
        _self_test_fields("date", "maturity"),
        ["date", "maturity", "rate"],
        frozenset(),
    ),
    (
        "a melt cannot demote the column the model computes",
        _SELF_TEST_MELT,
        _self_test_fields("date", "maturity", "maturity_years"),
        ["date", "maturity", "maturity_years", "rate"],
        frozenset({"maturity_years"}),
    ),
    (
        "a raw frame key keeps the field its alias names",
        _SELF_TEST_ALIAS,
        _self_test_fields("symbol", "name", "code", "exchange", "currency"),
        ["symbol", "name", "code"],
        frozenset(),
    ),
    (
        "a rename inside a nested helper closes nothing",
        _SELF_TEST_HELPER,
        _self_test_fields("date", "event", "country", "importance"),
        ["date", "event", "country", "importance"],
        frozenset(),
    ),
)


def run_self_test() -> tuple[int, int, list[str]]:
    """Show the emit rule both keeps and drops, arm by arm.

    Returns the number of arms, the number that behaved as expected, and a note for each that did
    not. A non-empty note list is a bug in this file, not in a provider.

    A reader that only ever printed a smaller column count would be unmeasured, so each arm here
    pins an exact answer that contains a name the rule must still require alongside the one it must
    demote, and two arms exist purely to prove the rule can decline to demote at all.
    """
    arms = 0
    fired = 0
    notes: list[str] = []
    for label, source, fields, expected, computed in _SELF_TEST_CASES:
        arms += 1
        tree = ast.parse(source, filename="<self-test>")
        produced, _ = _output_columns_of(tree, fields, computed)
        if produced == expected:
            fired += 1
        else:
            notes.append(f"{label}: got {produced}, expected {expected}")
    return arms, fired, notes


def main(argv: list[str] | None = None) -> int:
    """Write the spec sheet, refusing to read an upstream tree that moved."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", type=Path, default=DEFAULT_UPSTREAM_PATH)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--provider", default=None)
    parser.add_argument("--require-counts", default="32,350", help="providers,models")
    args = parser.parse_args(argv)

    arms, fired, notes = run_self_test()
    print(f"self-test: emit rule proven on both sides, arms={arms} fired={fired}")
    for note in notes:
        print(f"  SELF-TEST BROKEN: {note}")
    if fired != arms or notes:
        print("VERDICT: SELF-TEST FAILED (this reader is not proven on both sides)")
        return 2

    git = shutil.which("git")
    if git is None:
        print("FAIL: git is unavailable, so the upstream pin cannot be read", file=sys.stderr)
        return 2
    head = subprocess.run(  # noqa: S603  # nosec B603 - literal argv, shell disabled
        [git, "-C", str(args.upstream.parent), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if head != UPSTREAM_COMMIT:
        print(f"FAIL: upstream HEAD {head} != pinned {UPSTREAM_COMMIT}", file=sys.stderr)
        return 2

    spec = extract(args.upstream)
    total = sum(data["declared_model_count"] for data in spec.values())
    expected_providers, expected_models = (int(x) for x in args.require_counts.split(","))
    if args.provider is None and (len(spec) != expected_providers or total != expected_models):
        print(
            f"FAIL: providers={len(spec)} expected={expected_providers} "
            f"models={total} expected={expected_models}",
            file=sys.stderr,
        )
        return 2

    payload = {
        "upstream_commit": UPSTREAM_COMMIT,
        "provider_count": len(spec),
        "model_count": total,
        "providers": spec if args.provider is None else {args.provider: spec[args.provider]},
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, sort_keys=True, indent=1) + "\n", encoding="utf-8")

    counts: dict[str, int] = {}
    basis: dict[str, int] = {}
    columns = 0
    for data in spec.values():
        for entry in data["models"].values():
            counts[entry["resolution"]] = counts.get(entry["resolution"], 0) + 1
            basis[entry["column_basis"]] = basis.get(entry["column_basis"], 0) + 1
            columns += len(entry["output_columns"])
    # ``built`` is the population whose column set the data path proved, so the count of demotions
    # is printed every time the sheet is written; a silent narrowing is not measurable afterwards.
    print(
        f"OK: providers={len(spec)} models={total} resolutions={counts} "
        f"column_basis={basis} output_columns={columns} -> {args.out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
