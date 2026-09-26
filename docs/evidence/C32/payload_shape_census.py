#!/usr/bin/env python3
"""Pair every endpoint the frontend e2e plane stubs with what the backend returns.

C32 / AC-11 + AC-17 measurement plane. Run by hand, not by ``make gate``: the
judgment plane for the e2e tree is ``scripts/quality/frontend_e2e_plane.py``,
and this script answers a different question — whether the fixtures that plane
drives were written against the payloads the server actually produces. It reads
source only. Nothing here calls a live backend, touches MySQL, or starts a
browser, so it never measures whether a value is *right*; it measures the shape
a running server would emit.

What it prints, per endpoint:
  * the handler's file and line range, resolved from the router decorators and
    the ``include_router`` prefixes in ``opendata/api/__init__.py``;
  * whether the handler returns the ``APIResponse`` envelope or a bare object
    (three handlers do, and the axios interceptor's tolerance is what hid it);
  * the top-level keys of ``data``, and of the list items inside it, resolved
    through the ``_*_to_dict`` helpers and the pydantic response models;
  * the matching entry in ``frontend/e2e/fixtures.ts``, and whether the fixture
    wraps the payload the same way the handler does.

The comparison is the point. A fixture that envelopes a bare handler is a stub
the browser would never have answered that way, and the test that passes on it
says nothing about the page. Three findings, each reddening the exit code:

  * ``ENVELOPE MISMATCH`` — fixture wraps, handler does not (or the reverse);
  * ``NO SUCH ROUTE`` — no handler answers the key the fixture is registered
    under, so the stub is holding a response for a request that never arrives;
  * ``STUB-TYPO`` — the key paired only because a route has a ``{param}`` at that
    segment, and a real route sits one or two characters away. Found by mutation,
    not by reading: misspelling ``categories`` used to pass this script silently,
    because segment matching absorbed the typo into ``/scripts/{script_id}``.

The limit worth stating: a URL made only of parameter segments (``/data/{a}/{b}``)
is a route FastAPI really does answer, so a fixture keyed to one pairs rather than
orphans. This script measures shape and pairing; whether the page issues that
request is the e2e plane's own question.
"""

from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
API_DIR: Final = REPO_ROOT / "opendata" / "api"
MODELS_DIR: Final = REPO_ROOT / "opendata" / "models"
SERVICES_DIR: Final = REPO_ROOT / "opendata" / "services"
FIXTURES: Final = REPO_ROOT / "frontend" / "e2e" / "fixtures.ts"
API_BASE: Final = "/api/v1"

METHOD_RE: Final = re.compile(r"router\.(get|post|put|delete)\(\s*['\"]([^'\"]*)['\"]")
STUB_RE: Final = re.compile(r"^\s*'(GET|POST|PUT|DELETE) (/api/v1/[^']*)':", re.M)
#: A plane entry that reuses another entry's value instead of building its own,
#: as ``tasksPlane`` does for ``GET /api/v1/scripts/``.
DELEGATE_RE: Final = re.compile(r"\[\s*'(GET|POST|PUT|DELETE) (/api/v1/[^']*)'\s*\]")


@dataclass
class Handler:
    """One route declaration and what its ``return`` hands back.

    Attributes:
        module: File the handler lives in, relative to the repo root.
        line: 1-based line of the ``@router.*`` decorator.
        end_line: Last line of the function body.
        method: ``, `GET`, `POST`, ...
        path: Full mounted path, prefix included.
        name: Function name.
        envelope: ``APIResponse``, ``bare(<class>)`` or ``stream``.
        data_keys: Top-level keys of the payload the client receives.
        item_keys: Keys of the elements of ``data.items``/``data.<list>``, if any.
        built_models: Response models the handler constructs, with their fields.
        notes: Anything a reader needs to not be misled by the two fields above.
    """

    module: str
    line: int
    end_line: int
    method: str
    path: str
    name: str
    envelope: str = "?"
    data_keys: list[str] = field(default_factory=list)
    item_keys: list[str] = field(default_factory=list)
    built_models: list[tuple[str, list[str]]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class Stub:
    """One ``fixtures.ts`` route entry.

    Attributes:
        key: The ``'GET /api/v1/...'`` literal.
        wrapped: True when the value is built with ``envelope(...)``.
        line: 1-based line of the entry.
        delegates: The key of another entry this one copies its value from, so
            the wrapper the census reads is that entry's, not ``None``.
    """

    key: str
    wrapped: bool
    line: int
    delegates: str | None = None


def dict_keys(node: ast.AST | None) -> list[str]:
    """String keys of a dict literal, or ``[]`` when it is not one."""
    if isinstance(node, ast.Dict):
        return [
            key.value if isinstance(key, ast.Constant) and isinstance(key.value, str) else "?"
            for key in node.keys
        ]
    return []


def literal_dict_from_call(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """Keys of every dict literal a function returns, first-seen order."""
    seen: list[str] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Return):
            for key in dict_keys(node.value):
                if key not in seen:
                    seen.append(key)
    return seen


def pydantic_fields(cls: ast.ClassDef) -> list[str]:
    """Annotated attribute names of a class — the JSON keys of a response model."""
    return [
        target.id
        for stmt in cls.body
        if isinstance(stmt, ast.AnnAssign)
        for target in [stmt.target]
        if isinstance(target, ast.Name)
    ]


def parse(path: Path) -> ast.Module:
    """Parse one source file."""
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def module_prefixes() -> dict[str, str]:
    """Map ``opendata/api/__init__.py`` router variables to their mount prefixes.

    ``include_router(scripts_router, ...)`` passes a Name and
    ``include_router(settings_api.router, ...)`` an attribute, so the key is the
    root of the dotted expression — the name :data:`ROUTER_MODULES` uses too. A
    missed key drops a prefix silently, and a census printing
    ``GET /api/v1/{table_id}/schema`` without its ``/tables`` pairs stubs with
    the wrong handler.
    """
    tree = parse(API_DIR / "__init__.py")
    prefixes: dict[str, str] = {}
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "include_router"
            and node.args
        ):
            continue
        root = ast.unparse(node.args[0]).split(".")[0]
        prefix = ""
        for kw in node.keywords:
            if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                prefix = str(kw.value.value)
                break
        prefixes[root] = prefix
    missing = sorted(set(ROUTER_MODULES) - set(prefixes))
    if missing:
        raise RuntimeError(f"no include_router prefix found for {missing}")
    return prefixes


def _callee_name(func: ast.expr) -> str:
    """The called name of a call expression — ``f`` for both ``f()`` and ``o.f()``."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _callee_root(func: ast.expr) -> str:
    """Leading name of a chained callee — ``UserResponse`` of ``UserResponse.model_validate``."""
    node = func
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else ""


#: ``opendata/api/__init__.py`` imports the routers under their own names, and
#: the module holding each one is what the census needs to open.
ROUTER_MODULES: Final = {
    "auth_router": "auth.py",
    "interfaces_router": "interfaces.py",
    "tasks_router": "tasks.py",
    "data_router": "data.py",
    "data_query_router": "data_query.py",
    "tables_router": "tables.py",
    "users_router": "users.py",
    "keys_router": "keys.py",
    "scripts_router": "scripts.py",
    "executions_router": "executions.py",
    "pipeline_router": "pipeline.py",
    "settings_api": "settings.py",
}


#: Where the census is allowed to look for a helper it cannot see in the handler
#: itself. Serializers live in ``api``/``services``/``models``; anything outside
#: is reported as unresolved rather than guessed at from a name match elsewhere.
SEARCH_ROOTS: Final = (API_DIR, SERVICES_DIR, MODELS_DIR)


def _source_files() -> list[Path]:
    """Every ``.py`` under :data:`SEARCH_ROOTS`."""
    return sorted(path for root in SEARCH_ROOTS for path in root.rglob("*.py"))


def to_dict_index() -> dict[str, list[str]]:
    """``<Model>.to_dict()`` key sets, read off the ORM classes."""
    index: dict[str, list[str]] = {}
    for path in sorted(MODELS_DIR.glob("*.py")):
        for node in parse(path).body:
            if not isinstance(node, ast.ClassDef):
                continue
            for item in node.body:
                if (
                    isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and item.name == "to_dict"
                ):
                    index[node.name] = literal_dict_from_call(item)
    return index


def function_index() -> dict[str, list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]]:
    """Functions under :data:`SEARCH_ROOTS` as ``name -> [(repo-relative file, node)]``.

    All definitions are kept, not one: ``get_execution_stats`` is the name of the
    API handler *and* of the service method it calls, and a name-keyed index that
    kept the first would send the census in a circle around itself.
    """
    index: dict[str, list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]] = {}
    for path in _source_files():
        rel = str(path.relative_to(REPO_ROOT))
        for node in ast.walk(parse(path)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                index.setdefault(node.name, []).append((rel, node))
    return index


def _pick(
    functions: dict[str, list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]],
    name: str,
    origin: str,
) -> tuple[str, ast.FunctionDef | ast.AsyncFunctionDef] | None:
    """One definition of ``name``, preferring a file other than ``origin``."""
    candidates = functions.get(name)
    if not candidates:
        return None
    elsewhere = [entry for entry in candidates if entry[0] != origin]
    return (elsewhere or candidates)[0]


def class_index() -> dict[str, ast.ClassDef]:
    """Classes under :data:`SEARCH_ROOTS`, by name, first definition wins."""
    index: dict[str, ast.ClassDef] = {}
    for path in _source_files():
        for node in ast.walk(parse(path)):
            if isinstance(node, ast.ClassDef):
                index.setdefault(node.name, node)
    return index


def api_class_names() -> set[str]:
    """Class names declared under ``opendata/api`` — the response models.

    A handler building one of these says what its payload elements are even when
    the census cannot follow the comprehension that produced them.
    """
    names: set[str] = set()
    for path in sorted(API_DIR.glob("*.py")):
        for node in parse(path).body:
            if isinstance(node, ast.ClassDef):
                names.add(node.name)
    return names


def constructed_models(
    fn: ast.FunctionDef | ast.AsyncFunctionDef, api_classes: set[str]
) -> list[str]:
    """Response models this handler builds, in first-seen order.

    ``APIResponse`` is the envelope and is reported as that, so naming it again
    as a built model would only repeat the same line for every route.
    """
    found: list[str] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            root = _callee_root(node.func)
            if root in api_classes and root != "APIResponse" and root not in found:
                found.append(root)
    return found


def resolve_data(
    value: ast.expr | None,
    to_dicts: dict[str, list[str]],
    functions: dict[str, list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]],
    classes: dict[str, ast.ClassDef],
    notes: list[str],
    within: ast.FunctionDef | ast.AsyncFunctionDef | None = None,
    seen: tuple[tuple[str, str], ...] = (),
    origin: str = "",
) -> tuple[list[str], list[str]]:
    """Read the ``data=`` payload into ``(top-level keys, item keys)``.

    Walks into a helper (``_execution_to_dict`` -> ``TaskExecution.to_dict``),
    into the value a local name was assigned in the function now being read
    (``data=stats`` where ``stats = service.get_stats(...)``), and into the
    function a call names. Each hop is printed as a note, so a reader can tell a
    key set the handler states from one the census followed through three files
    to reach.

    A hop into a name resolves to a definition in another file when one exists
    (``_pick``), because the handler and the service method it calls can share a
    name. ``seen`` therefore records the *definitions entered*, as ``(file, name)``
    pairs: keying it on the bare call name refused the very hop ``_pick`` exists to
    take, and left ``GET /api/v1/executions/stats`` reporting no keys at all.
    """
    if value is None:
        return [], []
    if len(seen) > 6:
        notes.append("resolution stopped after 6 hops")
        return [], []
    if isinstance(value, ast.Await):
        return resolve_data(value.value, to_dicts, functions, classes, notes, within, seen, origin)
    top: list[str] = []
    items: list[str] = []
    if isinstance(value, ast.Dict):
        top = dict_keys(value)
        for key_node, val_node in zip(value.keys, value.values, strict=False):
            if not (isinstance(key_node, ast.Constant) and isinstance(key_node.value, str)):
                continue
            element, iterable = _list_element(val_node, within)
            if element is None:
                continue
            keys = _keys_of_element(
                element, to_dicts, functions, classes, notes, within, origin, iterable
            )
            if keys:
                items = keys
                notes.append(f"items of data.{key_node.value}")
            else:
                notes.append(
                    f"data.{key_node.value} is a list whose element shape this census cannot read"
                )
        return top, items
    if isinstance(value, ast.Call):
        name = _callee_name(value.func)
        entry = _pick(functions, name, origin)
        if entry is None:
            notes.append(f"data= {name}() unresolved (not under api/services/models)")
            return top, items
        rel, target = entry
        if (rel, target.name) in seen:
            notes.append(
                f"data= {name}() at {rel}:{target.lineno} is already on the resolution stack"
                " — not followed"
            )
            return top, items
        model = _serializer_model(target)
        if model in to_dicts:
            notes.append(f"data= via {name}() at {rel}:{target.lineno} -> {model}.to_dict()")
            return to_dicts[model], items
        inner, _ = resolve_data(
            _first_return(target),
            to_dicts,
            functions,
            classes,
            notes,
            target,
            (*seen, (rel, target.name)),
            rel,
        )
        notes.append(f"data= built by {name}() at {rel}:{target.lineno}")
        return inner, items
    if isinstance(value, ast.Name):
        assigned = _assigned_value(within, value.id)
        if assigned is None:
            notes.append(f"data= {value.id} (no assignment readable in the handler)")
            return top, items
        keys, list_items = resolve_data(
            assigned, to_dicts, functions, classes, notes, within, seen, origin
        )
        notes.append(f"data= {value.id} followed to its assignment")
        return keys, list_items
    if isinstance(value, (ast.ListComp, ast.GeneratorExp)):
        keys = _keys_of_element(
            value.elt,
            to_dicts,
            functions,
            classes,
            notes,
            within,
            origin,
            _for_iterable(value),
        )
        notes.append("data is a bare list")
        return [], keys
    notes.append(f"data= a {type(value).__name__} expression — keys unreadable statically")
    return top, items


def _assigned_value(
    fn: ast.FunctionDef | ast.AsyncFunctionDef | None, name: str
) -> ast.expr | None:
    """The value ``name`` was last assigned in ``fn``, or ``None`` if never."""
    if fn is None:
        return None
    found: ast.expr | None = None
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            found = node.value
    if found is None:
        for node in ast.walk(fn):
            if (
                isinstance(node, (ast.AnnAssign, ast.AugAssign))
                and isinstance(node.target, ast.Name)
                and node.target.id == name
            ):
                found = node.value
    return found


def _first_return(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> ast.expr | None:
    """The value of the first ``return <expr>`` in ``fn``."""
    for node in ast.walk(fn):
        if isinstance(node, ast.Return) and node.value is not None:
            return node.value
    return None


def _serializer_model(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """The ORM class a ``_x_to_dict`` helper serializes, or ``''`` if it is not one.

    Only a function whose body is ``return <arg>.to_dict()`` with ``<arg>``
    annotated by a class name qualifies. A looser test — "its first parameter has
    an annotation" — would read a service function that merely takes a model and
    returns computed statistics as if it emitted that model's columns.
    """
    returned = _first_return(fn)
    if not (
        isinstance(returned, ast.Call)
        and isinstance(returned.func, ast.Attribute)
        and returned.func.attr == "to_dict"
        and isinstance(returned.func.value, ast.Name)
    ):
        return ""
    receiver = returned.func.value.id
    for arg in fn.args.args:
        if arg.arg == receiver and isinstance(arg.annotation, ast.Name):
            return arg.annotation.id
    return ""


def _element_expr(value: ast.expr | None) -> ast.expr | None:
    """The expression producing one element of a list-valued payload entry."""
    if value is None:
        return None
    if isinstance(value, (ast.ListComp, ast.GeneratorExp)):
        return value.elt
    if isinstance(value, ast.List) and value.elts:
        return value.elts[0]
    return None


def _for_iterable(value: ast.expr | None) -> ast.expr | None:
    """What a comprehension iterates over, which is where its variable comes from."""
    if isinstance(value, (ast.ListComp, ast.GeneratorExp)) and value.generators:
        return value.generators[0].iter
    return None


def _list_element(
    value: ast.expr, within: ast.FunctionDef | ast.AsyncFunctionDef | None
) -> tuple[ast.expr | None, ast.expr | None]:
    """``(one element of a payload entry, what its list iterates)`` or ``(None, None)``.

    A payload entry can hold the comprehension inline (``"failures": [{...} for row
    in rows]``) or a name assigned to one a few lines above (``items = [...]``, then
    ``"items": items``). Reading only the inline form left most of this repo's
    handlers reporting no item keys, silently, because their list was a name.
    """
    source = value
    if isinstance(value, ast.Name):
        assigned = _assigned_value(within, value.id)
        if isinstance(assigned, (ast.ListComp, ast.GeneratorExp, ast.List)):
            source = assigned
    if not isinstance(source, (ast.ListComp, ast.GeneratorExp, ast.List)):
        return None, None
    return _element_expr(source), _for_iterable(source)


def _receiver_name(element: ast.Call) -> str:
    """The ``x`` of an ``x.something(...)`` call, when the receiver is a plain name."""
    func = element.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return func.value.id
    return ""


#: Methods that turn a pydantic model into the dict a payload holds. A list
#: element built with one of these carries its model's fields, not the argument
#: list of the call.
DUMP_METHODS: Final[frozenset[str]] = frozenset({"model_dump", "model_dump_json", "dict", "json"})


def _keys_of_element(
    element: ast.expr,
    to_dicts: dict[str, list[str]],
    functions: dict[str, list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]],
    classes: dict[str, ast.ClassDef],
    notes: list[str],
    within: ast.FunctionDef | ast.AsyncFunctionDef | None = None,
    origin: str = "",
    iterable: ast.expr | None = None,
    depth: int = 0,
) -> list[str]:
    """Keys one list element carries, through the serializer that builds it.

    ``iterable`` is what the comprehension holding ``element`` loops over, so the
    element can name its variable (``item.model_dump()``) and still be traced to
    the list ``item`` comes from. Without that hop the two biggest paginated
    handlers in this repo, ``GET /api/v1/tables/`` and ``GET /api/v1/scripts/``,
    reported their envelope and no item keys at all.
    """
    if isinstance(element, ast.Call):
        name = _callee_name(element.func)
        receiver = _receiver_name(element)
        if depth > 3:
            notes.append("element hop cap reached — the item shape is not reported")
            return []
        if name in DUMP_METHODS:
            if receiver in classes:
                notes.append(f"element keys from {receiver} response model fields")
                return pydantic_fields(classes[receiver])
            source: ast.expr | None = _assigned_value(within, receiver) if receiver else None
            if source is None and isinstance(iterable, ast.Name):
                source = _assigned_value(within, iterable.id)
            inner = _element_expr(source)
            if inner is not None:
                notes.append(f"element is {receiver}.{name}(), {receiver} read one hop further")
                return _keys_of_element(
                    inner,
                    to_dicts,
                    functions,
                    classes,
                    notes,
                    within,
                    origin,
                    _for_iterable(source),
                    depth + 1,
                )
            notes.append(
                f"element dumped from {receiver or name}(), which this census cannot place"
            )
            return []
        entry = _pick(functions, name, origin)
        if entry is not None:
            _, helper = entry
            annotation = _serializer_model(helper)
            if annotation in to_dicts:
                notes.append(f"element keys from {annotation}.to_dict()")
                return to_dicts[annotation]
            keys = literal_dict_from_call(helper)
            if keys:
                notes.append(f"element keys from {name}() literal")
            return keys
        if receiver in classes or name in classes:
            model = receiver if receiver in classes else name
            notes.append(f"element keys from {model} response model fields")
            return pydantic_fields(classes[model])
        notes.append(f"element built by unresolved {name}()")
        return []
    if isinstance(element, ast.Attribute) and element.attr == "model_dump":
        return []
    keys = dict_keys(element)
    if keys:
        notes.append("element keys from an inline dict literal")
    return keys


def envelope_of(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[str, ast.expr | None]:
    """Classify a handler's return and hand back its ``data`` payload expression."""
    returns = [node for node in ast.walk(fn) if isinstance(node, ast.Return)]
    if not returns:
        return "no-return", None
    values = [node.value for node in returns if node.value is not None]
    if any(isinstance(v, ast.Call) and _callee_name(v.func) == "StreamingResponse" for v in values):
        return "stream", None
    calls = [v for v in values if isinstance(v, ast.Call)]
    names = {_callee_root(c.func) for c in calls}
    if "APIResponse" in names:
        data_kwargs = [
            kw.value
            for c in calls
            for kw in c.keywords
            if kw.arg == "data" and _callee_name(c.func) == "APIResponse"
        ]
        if not data_kwargs:
            return "APIResponse", None
        return "APIResponse", data_kwargs[0]
    if len(names) == 1 and "APIResponse" not in names:
        only = next(iter(names))
        return f"bare({only})", None
    return f"mixed({','.join(sorted(names))})", None


def handlers() -> list[Handler]:
    """Every declared route in ``opendata/api``, with its payload shape."""
    prefixes = module_prefixes()
    to_dicts = to_dict_index()
    functions = function_index()
    classes = class_index()
    api_classes = api_class_names()
    out: list[Handler] = []
    for router_var, filename in sorted(ROUTER_MODULES.items()):
        path = API_DIR / filename
        if not path.is_file():
            continue
        prefix = prefixes.get(router_var, "")
        tree = parse(path)
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            decorators = [ast.unparse(d) for d in node.decorator_list]
            matched = [
                (m.group(1).upper(), m.group(2))
                for d in decorators
                for m in [METHOD_RE.search(d)]
                if m
            ]
            for method, raw_path in matched:
                notes: list[str] = []
                envelope, data_expr = envelope_of(node)
                top: list[str] = []
                items: list[str] = []
                if data_expr is not None:
                    module_rel = str(path.relative_to(REPO_ROOT))
                    top, items = resolve_data(
                        data_expr,
                        to_dicts,
                        functions,
                        classes,
                        notes,
                        node,
                        ((module_rel, node.name),),
                        module_rel,
                    )
                elif envelope.startswith("bare("):
                    model = envelope[len("bare(") : -1]
                    if model in classes:
                        top = pydantic_fields(classes[model])
                        notes.append(f"bare object: the handler returns a {model} instance")
                    elif node.returns is not None:
                        ann = ast.unparse(node.returns)
                        envelope = f"bare(<{ann}>)"
                        notes.append(
                            f"bare object: built outside the parsed roots, "
                            f"declared return type is {ann}"
                        )
                    else:
                        notes.append("bare object: shape unreadable statically")
                full = f"{API_BASE}{prefix}{raw_path}"
                out.append(
                    Handler(
                        module=str(path.relative_to(REPO_ROOT)),
                        line=node.lineno,
                        end_line=node.end_lineno or node.lineno,
                        method=method,
                        path=full,
                        name=node.name,
                        envelope=envelope,
                        data_keys=top,
                        item_keys=items,
                        built_models=[
                            (model, pydantic_fields(classes[model]))
                            for model in constructed_models(node, api_classes)
                            if model in classes
                        ],
                        notes=notes,
                    )
                )
    return out


def read_stubs() -> list[Stub]:
    """Every ``'METHOD /api/v1/...'`` key in the e2e fixtures, and how it wraps."""
    text = FIXTURES.read_text(encoding="utf-8")
    stubs: list[Stub] = []
    for match in STUB_RE.finditer(text):
        line = text[: match.start()].count("\n") + 1
        body = _entry_body(text, match.end())
        delegate = DELEGATE_RE.search(body)
        stubs.append(
            Stub(
                key=f"{match.group(1)} {match.group(2)}",
                wrapped="envelope(" in body,
                line=line,
                delegates=(f"{delegate.group(1)} {delegate.group(2)}" if delegate else None),
            )
        )
    return stubs


def effective_wrapping(stub: Stub, by_key: dict[str, Stub]) -> tuple[bool, str]:
    """Whether the stub sends an envelope, following one reuse hop.

    ``tasksPlane`` answers ``GET /scripts/`` by copying the value ``scriptsPlane``
    built. Reading that entry on its own says "no ``envelope(`` here" and reports
    a mismatch against a handler the fixture actually wraps correctly — a finding
    the census would invent. ``by_key`` is first-entry-wins, so the copy resolves
    to the plane that built the value rather than to the copy itself; a reuse it
    cannot resolve is reported as one rather than guessed at.
    """
    if stub.delegates is None:
        return stub.wrapped, ""
    target = by_key.get(stub.delegates)
    if target is None or target is stub:
        return stub.wrapped, f"reuses {stub.delegates}, which was not resolved"
    return target.wrapped, f"reuses {stub.delegates} (fixtures.ts:{target.line})"


def _entry_body(text: str, start: int) -> str:
    """The fixture entry's value, up to the next entry key."""
    nxt = STUB_RE.search(text, start)
    return text[start : nxt.start() if nxt else len(text)]


def normalize(path: str) -> list[str]:
    """Split a path into segments for matching `{param}` against an id."""
    return [segment for segment in path.split("/") if segment]


def _is_param(segment: str) -> bool:
    """True for a `{name}` path segment."""
    return segment.startswith("{") and segment.endswith("}") and len(segment) > 2


def _score(handler: Handler, stub: Stub) -> int | None:
    """Literal segments agreeing between a route and a stub key, or ``None``."""
    method, _, raw = stub.key.partition(" ")
    if method != handler.method:
        return None
    want = normalize(handler.path)
    got = normalize(raw)
    if len(got) != len(want):
        return None
    pairs = list(zip(want, got, strict=True))
    if not all(a == b or _is_param(a) for a, b in pairs):
        return None
    return sum(1 for a, b in pairs if a == b)


def best_handler(stub: Stub, all_handlers: list[Handler]) -> Handler | None:
    """The route a fixtures entry answers, preferring the most literal match.

    ``/scripts/{script_id}`` and ``/scripts/categories`` are the same length and
    both match the stub for ``categories``, so the pairing has to be scored
    rather than taken from whichever handler the file declares first — the
    census prints a file:line, and an accidental pairing would cite a handler
    nobody called.
    """
    scored = [(score, h) for h in all_handlers if (score := _score(h, stub)) is not None]
    if not scored:
        return None
    return max(scored, key=lambda item: (item[0], -item[1].path.count("{")))[1]


def _distance(one: str, other: str) -> int:
    """Levenshtein distance between two short path segments."""
    if len(one) < len(other):
        one, other = other, one
    previous = list(range(len(other) + 1))
    for i, char in enumerate(one, start=1):
        current = [i]
        for j, target in enumerate(other, start=1):
            current.append(
                min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (char != target))
            )
        previous = current
    return previous[-1]


def param_near_misses(stub: Stub, handler: Handler, all_handlers: list[Handler]) -> list[str]:
    """Stub segments that paired only because the route has a ``{param}`` there.

    ``GET /api/v1/scripts/categoires`` matches ``GET /api/v1/scripts/{script_id}``
    under segment matching, so a misspelled fixture key reads as a pairing instead
    of as the orphan it is — and a fixture keyed to a URL no page requests stops
    stubbing the very request the test is waiting for. A real route one or two
    characters away at that same position is the tell.

    Proven by mutation, not by reading: misspelling ``categories`` above takes the
    census from ``0`` findings to ``1`` and exit ``1``.
    """
    route = normalize(handler.path)
    got = normalize(stub.key.partition(" ")[2])
    out: list[str] = []
    for index, segment in enumerate(route):
        if not _is_param(segment):
            continue
        want = got[index]
        for other in all_handlers:
            if other is handler or other.method != handler.method:
                continue
            parts = normalize(other.path)
            if len(parts) != len(route) or _is_param(parts[index]):
                continue
            if any(pos != index and parts[pos] != got[pos] for pos in range(len(parts))):
                continue
            away = _distance(parts[index], want)
            if 0 < away <= 2:
                out.append(
                    f"'{want}' matches no route, but {other.method} {other.path} is {away} "
                    "character(s) away"
                )
    return sorted(set(out))


def main() -> int:
    """Print the census and fail on any of the three ways a fixture can be fiction.

    Returns:
        ``0`` every stub pairs with a route, under the same envelope the handler
        returns, and by its own literal path; ``1`` otherwise.
    """
    all_handlers = handlers()
    stubs = read_stubs()
    print(f"routes parsed            : {len(all_handlers)}")
    print(f"fixture stubs parsed     : {len(stubs)}")
    print("")

    mismatched = 0
    orphaned = 0
    mistyped = 0
    by_key: dict[str, Stub] = {}
    for stub in stubs:
        by_key.setdefault(stub.key, stub)
    for stub in stubs:
        found = best_handler(stub, all_handlers)
        wrapped, reuse = effective_wrapping(stub, by_key)
        label = "envelope" if wrapped else "BARE object"
        if found is None:
            print(f"{stub.key}")
            print(f"  fixture              : {label} (fixtures.ts:{stub.line})")
            print("  backend              : NO SUCH ROUTE — the stub has no handler to match")
            orphaned += 1
            continue
        for miss in param_near_misses(stub, found, all_handlers):
            print(f"{stub.key}   ->  {found.module}:{found.line}-{found.end_line} {found.name}")
            print(f"  *** STUB-TYPO — {miss}")
            mistyped += 1
        wrapped_backend = found.envelope == "APIResponse"
        agree = wrapped_backend == wrapped
        print(f"{stub.key}   ->  {found.module}:{found.line}-{found.end_line} {found.name}")
        print(f"  backend              : {found.envelope}")
        if found.data_keys:
            print(f"  data keys            : {', '.join(found.data_keys)}")
        if found.item_keys:
            print(f"  item keys            : {', '.join(found.item_keys)}")
        for model, fields in found.built_models:
            print(f"  built model          : {model}({', '.join(fields)})")
        for note in dict.fromkeys(found.notes):
            print(f"  note                 : {note}")
        print(f"  fixture              : {label} (fixtures.ts:{stub.line})")
        if reuse:
            print(f"  fixture note         : {reuse}")
        if not agree:
            print("  *** ENVELOPE MISMATCH — the browser would not have been answered this way")
            mismatched += 1
        print("")

    bare = [
        h
        for h in all_handlers
        if h.envelope not in {"APIResponse", "stream", "no-return"}
        and not h.envelope.startswith("mixed(")
    ]
    print("== handlers that do not return the APIResponse envelope ==")
    for handler in sorted(bare, key=lambda item: item.path):
        hit = "stubbed" if any(_score(handler, s) is not None for s in stubs) else "-"
        print(
            f"  {handler.method} {handler.path}  {handler.envelope}  "
            f"{handler.module}:{handler.line}  [{hit}]"
        )
    print("")
    print(f"envelope disagreements   : {mismatched}")
    print(f"stubs with no route      : {orphaned}")
    print(f"stubs matched by typo    : {mistyped}")
    return 1 if mismatched or orphaned or mistyped else 0


if __name__ == "__main__":
    sys.exit(main())
