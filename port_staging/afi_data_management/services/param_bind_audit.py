"""Static, AST-only binder audit for akshare scheduled task parameters.

The scheduler runs a script by resolving one callable from the ``ak_data_scripts``
row and calling it with keyword arguments only::

    result = await self._execute_callable(callable_obj, params, ...)
    awaitable = callable_obj(**params)          # app/services/akshare/script.py

A task whose ``parameters`` contain a key the resolved entrypoint does not accept
fails at runtime with ``TypeError: ... got an unexpected keyword argument`` -- the
``EtfFundHistEm.run() got an unexpected keyword argument 'source'`` failure. Nothing
validates that binding before the run, so this module answers the question
statically.

Design contract:

* **No imports.** Collection scripts may open sockets or the database at import
  time, so entrypoints are resolved by parsing source with :mod:`ast` only.
* **Mirror the driver, do not invent a rule.** Callable resolution reproduces
  ``AkshareScriptService._resolve_callable`` and its helper
  ``_resolve_module_callable`` in the same order: module-level ``func_name``
  (unless it is ``main``), then the first legacy class instance defined in that
  module (source order) offering ``func_name``/``fetch_data``/``run``, then
  module-level ``func_name``, then module-level ``fetch_data``/``run``/``main``,
  then the ``ak_data_interfaces`` fallback, then failure.
* ``params`` are the runtime params, i.e. task keys merged with the driver's
  ``_apply_safe_default_parameters`` injection for that ``script_id`` (extracted
  from the driver source too, never copied).

Exactly one classification per task (precedence top-down; a proven runtime failure
outranks a weaker-evidence class):

``script_file_absent``
    The registered ``module_path`` has no file in the audited tree.
``unresolvable:<reason>``
    The entrypoint cannot be bound statically (opaque alias/import, unanalysable
    base class, unparsable file, non-JSON parameters, no callable at all).
``missing_required_param:<name>``
    A required parameter is absent from the runtime keys. Proven even when the
    entrypoint has ``**kwargs``.
``accepts_var_kwargs``
    The entrypoint accepts ``**kwargs``, so unexpected keys cannot be disproved
    statically. Kept as its own class so the unverifiable surface stays a number
    instead of being silently absorbed into ``binds``.
``unexpected_param:<name>``
    A runtime key cannot bind to any parameter of a signature without ``**kwargs``.
``binds``
    Every runtime key binds and every required parameter is supplied.

Positional-only parameters are treated as unbindable by keyword, matching CPython
(``f(**params)`` can never fill them): absent -> ``missing_required_param``, present
-> ``unexpected_param``. Decorated entrypoints are classified from the undecorated
``def`` signature and flagged in ``notes``.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal

__all__ = [
    "CLASS_ACCEPTS_VAR_KWARGS",
    "CLASS_BINDS",
    "CLASS_MISSING_REQUIRED_PARAM",
    "CLASS_SCRIPT_FILE_ABSENT",
    "CLASS_UNEXPECTED_PARAM",
    "CLASS_UNRESOLVABLE",
    "BindingVerdict",
    "ClassSyntax",
    "FunctionSyntax",
    "InterfaceRow",
    "ModuleSyntax",
    "ParameterSpec",
    "PythonTreeSource",
    "ResolvedEntrypoint",
    "ResolvedSignature",
    "TaskAuditResult",
    "TaskRecord",
    "UnresolvedEntrypoint",
    "audit_task",
    "classify_binding",
    "driver_safe_default_param_keys",
    "normalize_task_parameters",
    "resolve_entrypoint",
]

ParameterKind = Literal["positional_only", "positional_or_keyword", "keyword_only"]

CLASS_BINDS: Final = "binds"
CLASS_UNEXPECTED_PARAM: Final = "unexpected_param"
CLASS_MISSING_REQUIRED_PARAM: Final = "missing_required_param"
CLASS_ACCEPTS_VAR_KWARGS: Final = "accepts_var_kwargs"
CLASS_SCRIPT_FILE_ABSENT: Final = "script_file_absent"
CLASS_UNRESOLVABLE: Final = "unresolvable"

_MODULE_LEVEL_FALLBACK_NAMES: Final = ("fetch_data", "run", "main")
_LEGACY_INSTANCE_NAMES: Final = ("fetch_data", "run")
_MAX_BASE_DEPTH: Final = 6


@dataclass(frozen=True)
class ParameterSpec:
    """One parameter of a statically resolved signature."""

    name: str
    kind: ParameterKind
    has_default: bool

    @property
    def keyword_bindable(self) -> bool:
        return self.kind != "positional_only"

    @property
    def required(self) -> bool:
        """A parameter a keyword-only call must supply.

        Positional-only parameters are always required for ``f(**params)`` because
        a keyword call can never fill them.
        """
        if self.kind == "positional_only":
            return True
        return not self.has_default


@dataclass(frozen=True)
class FunctionSyntax:
    """A ``def``/``async def`` found in the audited tree."""

    name: str
    line: int
    parameters: tuple[ParameterSpec, ...]
    has_var_positional: bool = False
    has_var_keyword: bool = False
    decorators: tuple[str, ...] = ()
    owner_class: str | None = None

    @property
    def qualified_name(self) -> str:
        return f"{self.owner_class}.{self.name}" if self.owner_class else self.name

    @property
    def is_decorated(self) -> bool:
        return bool(self.decorators)

    def accepts_keyword(self, key: str) -> bool:
        return any(param.name == key and param.keyword_bindable for param in self.parameters)

    def is_positional_only(self, name: str) -> bool:
        return any(
            param.name == name and param.kind == "positional_only" for param in self.parameters
        )

    def missing_required_keys(self, supplied: frozenset[str]) -> tuple[str, ...]:
        return tuple(
            param.name for param in self.parameters if param.required and param.name not in supplied
        )


@dataclass(frozen=True)
class ClassSyntax:
    """A module-level class definition."""

    name: str
    line: int
    methods: dict[str, FunctionSyntax]
    class_attributes: frozenset[str]
    bases: tuple[str, ...] = ()

    def attribute_only(self, name: str) -> bool:
        """``hasattr`` is satisfied by a non-method class attribute."""
        return name in self.class_attributes and name not in self.methods


@dataclass(frozen=True)
class ImportTarget:
    """Where an in-file name came from, when it came from an import."""

    module_name: str
    attribute: str | None


@dataclass(frozen=True)
class ResolvedModuleCallable:
    """A module-level attribute known to be callable, or known to be opaque."""

    function: FunctionSyntax | None = None
    constructor: ClassSyntax | None = None
    opaque: str | None = None


@dataclass(frozen=True)
class ModuleSyntax:
    """Everything the driver's ``getattr`` checks need about one file."""

    module_name: str
    path: Path
    functions: dict[str, FunctionSyntax]
    classes: dict[str, ClassSyntax]
    class_order: tuple[str, ...]
    imports: dict[str, ImportTarget]
    opaque_names: frozenset[str]
    prefer_local_script: bool
    parse_error: str | None = None

    def module_level_callable(self, name: str) -> ResolvedModuleCallable | None:
        """Mirror ``callable(getattr(module, name))`` for module-level names."""
        function = self.functions.get(name)
        if function is not None:
            return ResolvedModuleCallable(function=function)
        klass = self.classes.get(name)
        if klass is not None:
            # A class is callable; calling it takes the constructor signature.
            return ResolvedModuleCallable(constructor=klass)
        if name in self.opaque_names or name in self.imports:
            return ResolvedModuleCallable(opaque=name)
        return None


@dataclass(frozen=True)
class ResolvedSignature:
    """The entrypoint signature plus the file:line a human has to triage."""

    callable_name: str
    file_path: Path
    line: int
    function: FunctionSyntax
    notes: tuple[str, ...] = ()

    @property
    def location(self) -> str:
        return f"{self.file_path}:{self.line}"


@dataclass(frozen=True)
class ResolvedEntrypoint:
    """A statically bound entrypoint."""

    signature: ResolvedSignature


@dataclass(frozen=True)
class UnresolvedEntrypoint:
    """Why the entrypoint could not be bound statically."""

    reason: str
    detail: str = ""


@dataclass(frozen=True)
class InterfaceRow:
    """The ``ak_data_interfaces`` fallback the driver may resolve instead."""

    module_path: str
    function_name: str


@dataclass(frozen=True)
class TaskRecord:
    """One armed task joined with its registered script row."""

    task_id: int
    script_id: str
    module_path: str | None
    function_name: str | None
    source: str | None
    is_custom: bool
    parameters: object
    interface_row: InterfaceRow | None = None

    @property
    def entrypoint_name(self) -> str:
        return str(self.function_name or "main")


@dataclass(frozen=True)
class BindingVerdict:
    """Classification of runtime keys against one resolved signature."""

    classification: str
    detail_key: str
    offending_keys: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class TaskAuditResult:
    """The per-task audit line emitted into the JSON report."""

    task_id: int
    script_id: str
    entrypoint_name: str
    module_path: str | None
    classification: str
    family: str
    detail_key: str
    runtime_param_keys: tuple[str, ...] = ()
    task_param_keys: tuple[str, ...] = ()
    injected_default_keys: tuple[str, ...] = ()
    offending_keys: tuple[str, ...] = ()
    signature_file: str = ""
    signature_line: int | None = None
    signature_callable: str = ""
    reason: str = ""
    notes: tuple[str, ...] = ()


class PythonTreeSource:
    """Parse-on-demand view of a source tree; never imports anything."""

    def __init__(self, package_root: Path) -> None:
        self._package_root = package_root
        self._cache: dict[str, ModuleSyntax | None] = {}

    @property
    def package_root(self) -> Path:
        return self._package_root

    def module_file(self, module_name: str) -> Path | None:
        """Return the file that would satisfy ``importlib.import_module``."""
        if not module_name or module_name.startswith("."):
            return None
        parts = module_name.split(".")
        candidate_module = self._package_root.joinpath(*parts).with_suffix(".py")
        if candidate_module.is_file():
            return candidate_module
        candidate_package = self._package_root.joinpath(*parts) / "__init__.py"
        if candidate_package.is_file():
            return candidate_package
        return None

    def syntax(self, module_name: str) -> ModuleSyntax | None:
        """Parse (and cache) a module by dotted name; ``None`` when absent."""
        if module_name in self._cache:
            return self._cache[module_name]
        path = self.module_file(module_name)
        if path is None:
            self._cache[module_name] = None
            return None
        parsed = _parse_module(path, module_name)
        self._cache[module_name] = parsed
        return parsed

    def syntax_of_file(self, path: Path, module_name: str) -> ModuleSyntax:
        """Parse one file directly; the driver resolves ``module_path`` to it."""
        cached = self._cache.get(module_name)
        if cached is not None and cached.path == path:
            return cached
        parsed = _parse_module(path, module_name)
        self._cache[module_name] = parsed
        return parsed


@dataclass
class _LookupState:
    """Mutable bookkeeping for inheritance walks."""

    source: PythonTreeSource
    visited: set[tuple[str, str]] = field(default_factory=set)
    incomplete_base: bool = False
    depth: int = 0
    reexport_hops: int = 0


def _dotted_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted_name(node.value)
        if base is not None:
            return f"{base}.{node.attr}"
    return None


def _decorator_names(decorators: list[ast.expr]) -> tuple[str, ...]:
    names: list[str] = []
    for decorator in decorators:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        names.append(_dotted_name(target) or "<dynamic>")
    return tuple(names)


def _parameter_specs(args: ast.arguments, *, is_method: bool) -> tuple[ParameterSpec, ...]:
    positional = [*args.posonlyargs, *args.args]
    defaults: list[ast.expr | None] = [None] * (len(positional) - len(args.defaults))
    defaults.extend(args.defaults)
    specs: list[ParameterSpec] = []
    for index, (arg, default) in enumerate(zip(positional, defaults, strict=True)):
        kind: ParameterKind = (
            "positional_only" if index < len(args.posonlyargs) else "positional_or_keyword"
        )
        if is_method and index == 0 and not arg.arg.startswith("__"):
            # Bound methods hide ``self``/``cls``; a keyword call cannot pass it.
            continue
        specs.append(ParameterSpec(name=arg.arg, kind=kind, has_default=default is not None))
    number_of_kw_defaults = len(args.kw_defaults)
    first_defaulted_kw = len(args.kwonlyargs) - number_of_kw_defaults
    for index, arg in enumerate(args.kwonlyargs):
        kw_default: ast.expr | None = None
        if index >= first_defaulted_kw:
            kw_default = args.kw_defaults[index - first_defaulted_kw]
        specs.append(
            ParameterSpec(name=arg.arg, kind="keyword_only", has_default=kw_default is not None)
        )
    return tuple(specs)


def _function_syntax(
    node: ast.FunctionDef | ast.AsyncFunctionDef, *, owner: str | None
) -> FunctionSyntax:
    return FunctionSyntax(
        name=node.name,
        line=node.lineno,
        parameters=_parameter_specs(node.args, is_method=owner is not None),
        has_var_positional=node.args.vararg is not None,
        has_var_keyword=node.args.kwarg is not None,
        decorators=_decorator_names(node.decorator_list),
        owner_class=owner,
    )


def _class_syntax(node: ast.ClassDef) -> ClassSyntax:
    methods: dict[str, FunctionSyntax] = {}
    attributes: set[str] = set()
    for item in node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            methods[item.name] = _function_syntax(item, owner=node.name)
        elif isinstance(item, ast.Assign):
            for target in item.targets:
                if isinstance(target, ast.Name):
                    attributes.add(target.id)
        elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
            attributes.add(item.target.id)
    bases = tuple(name for name in (_dotted_name(base) for base in node.bases) if name is not None)
    return ClassSyntax(
        name=node.name,
        line=node.lineno,
        methods=methods,
        class_attributes=frozenset(attributes),
        bases=bases,
    )


def _relative_module(current_module: str, node: ast.ImportFrom) -> str | None:
    if node.level == 0:
        return node.module
    parts = current_module.split(".")
    package = parts[: max(len(parts) - node.level, 0)]
    tail = node.module.split(".") if node.module else []
    joined = ".".join([*package, *tail])
    return joined or None


def _parse_module(path: Path, module_name: str) -> ModuleSyntax:
    functions: dict[str, FunctionSyntax] = {}
    classes: dict[str, ClassSyntax] = {}
    class_order: list[str] = []
    imports: dict[str, ImportTarget] = {}
    assigned: dict[str, ast.expr] = {}
    prefer_local_script = False
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError) as exc:
        return ModuleSyntax(
            module_name=module_name,
            path=path,
            functions={},
            classes={},
            class_order=(),
            imports={},
            opaque_names=frozenset(),
            prefer_local_script=False,
            parse_error=f"{type(exc).__name__}: {exc}",
        )

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions[node.name] = _function_syntax(node, owner=None)
        elif isinstance(node, ast.ClassDef):
            classes[node.name] = _class_syntax(node)
            class_order.append(node.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.asname or alias.name.split(".")[0]
                imports[name] = ImportTarget(module_name=alias.name, attribute=None)
        elif isinstance(node, ast.ImportFrom):
            module = _relative_module(module_name, node)
            if module is None:
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                name = alias.asname or alias.name
                imports[name] = ImportTarget(module_name=module, attribute=alias.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assigned[target.id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.value is not None:
                assigned[node.target.id] = node.value

    opaque: set[str] = set()
    for name, value in assigned.items():
        if name == "PREFER_LOCAL_SCRIPT":
            if isinstance(value, ast.Constant) and isinstance(value.value, bool):
                prefer_local_script = value.value
            else:
                prefer_local_script = not _is_falsy_constant(value)
            continue
        if name in functions or name in classes:
            continue
        if _is_possible_callable_value(value, imports):
            opaque.add(name)

    for name in imports:
        if name not in functions and name not in classes:
            opaque.add(name)

    return ModuleSyntax(
        module_name=module_name,
        path=path,
        functions=functions,
        classes=classes,
        class_order=tuple(class_order),
        imports=imports,
        opaque_names=frozenset(opaque),
        prefer_local_script=prefer_local_script,
    )


def _is_falsy_constant(value: ast.expr) -> bool:
    return isinstance(value, ast.Constant) and not value.value


def _is_possible_callable_value(value: ast.expr, imports: Mapping[str, ImportTarget]) -> bool:
    """Detect ``main = something`` aliases whose signature is not in this file."""
    if isinstance(value, ast.Name):
        return value.id in imports
    if isinstance(value, (ast.Attribute, ast.Call, ast.Lambda)):
        return True
    return False


def _class_from_target(
    state: _LookupState, target: ImportTarget
) -> tuple[ModuleSyntax, ClassSyntax] | None:
    if target.attribute is None:
        return None
    if state.reexport_hops >= _MAX_BASE_DEPTH:
        # ``from .a import B`` chains that re-export each other; stop rather than recurse.
        state.incomplete_base = True
        return None
    state.reexport_hops += 1
    try:
        module = state.source.syntax(target.module_name)
        if module is None or module.parse_error is not None:
            state.incomplete_base = True
            return None
        klass = module.classes.get(target.attribute)
        if klass is not None:
            return module, klass
        nested = module.imports.get(target.attribute)
        if nested is None:
            state.incomplete_base = True
            return None
        return _class_from_target(state, nested)
    finally:
        state.reexport_hops -= 1


def _resolve_imported_class(
    state: _LookupState, module: ModuleSyntax, base_name: str
) -> tuple[ModuleSyntax, ClassSyntax] | None:
    parts = base_name.split(".")
    head = parts[0]
    target = module.imports.get(head)
    if target is None:
        return None
    if len(parts) > 1:
        # ``from pkg import submodule`` followed by ``submodule.Class``.
        submodule = state.source.syntax(target.module_name)
        if submodule is None:
            state.incomplete_base = True
            return None
        attribute = parts[-1]
        klass = submodule.classes.get(attribute)
        if klass is not None:
            return submodule, klass
        nested = submodule.imports.get(attribute)
        if nested is None:
            state.incomplete_base = True
            return None
        return _class_from_target(state, nested)
    return _class_from_target(state, target)


def _find_method(
    state: _LookupState, module: ModuleSyntax, klass: ClassSyntax, name: str
) -> tuple[FunctionSyntax, ModuleSyntax] | None:
    method = klass.methods.get(name)
    if method is not None:
        return method, module
    key = (module.module_name, klass.name)
    if key in state.visited or state.depth >= _MAX_BASE_DEPTH:
        return None
    state.visited.add(key)
    state.depth += 1
    found: tuple[FunctionSyntax, ModuleSyntax] | None = None
    try:
        for base in klass.bases:
            resolved = _resolve_imported_class(state, module, base)
            if resolved is None:
                continue
            found = _find_method(state, resolved[0], resolved[1], name)
            if found is not None:
                break
    finally:
        state.depth -= 1
    return found


def _lookup_method(
    module: ModuleSyntax, klass: ClassSyntax, name: str, source: PythonTreeSource
) -> tuple[FunctionSyntax, ModuleSyntax, bool] | None:
    """Mirror ``getattr(instance, name)`` through locally analyzable bases only."""
    if klass.attribute_only(name):
        return None
    state = _LookupState(source=source)
    found = _find_method(state, module, klass, name)
    if found is None:
        return None
    return found[0], found[1], state.incomplete_base


def _class_is_instantiable(
    module: ModuleSyntax, klass: ClassSyntax, source: PythonTreeSource
) -> tuple[bool, str]:
    """Mirror ``obj()`` inside ``_legacy_script_instance`` (raises -> class skipped)."""
    found = _lookup_method(module, klass, "__init__", source)
    if found is None:
        if klass.bases:
            return True, "constructor_inherited_unverified"
        return True, ""
    function, _, incomplete = found
    if function.missing_required_keys(frozenset()) and not function.has_var_positional:
        return False, "init_requires_argument"
    if incomplete:
        return True, "constructor_from_unanalysable_base"
    return True, ""


def _legacy_instance_callable(
    module: ModuleSyntax, func_name: str, source: PythonTreeSource
) -> ResolvedSignature | None:
    """Mirror ``_legacy_script_instance`` plus the ``(func_name, fetch_data, run)`` scan.

    ``None`` means "no usable legacy instance callable", exactly like the driver, so the
    caller continues to the module-level fallbacks. Only the first class that passes the
    ``hasattr(fetch_data or run)`` test and instantiates is considered, because the
    driver builds one instance and never retries another class after a successful build.
    """
    for class_name in module.class_order:
        klass = module.classes[class_name]
        # ``hasattr(cls, name)`` is satisfied by methods, class attributes and anything
        # an inherited base adds.
        eligible = any(
            klass.attribute_only(name) or _lookup_method(module, klass, name, source) is not None
            for name in _LEGACY_INSTANCE_NAMES
        )
        if not eligible:
            continue
        instantiable, init_note = _class_is_instantiable(module, klass, source)
        if not instantiable:
            # ``obj()`` raised TypeError for a required constructor argument; the driver
            # catches it and keeps scanning.
            continue
        notes: list[str] = []
        if init_note:
            notes.append(f"constructor={init_note}")
        for name in (func_name, *_LEGACY_INSTANCE_NAMES):
            found = _lookup_method(module, klass, name, source)
            if found is None:
                continue
            function, defining_module, incomplete = found
            if incomplete:
                # The method came through a base we could not read: attribute names are
                # known, but the defining signature line may be elsewhere.
                notes.append("base_method_defined_outside_class")
            return ResolvedSignature(
                callable_name=f"{class_name}.{name}",
                file_path=defining_module.path,
                line=function.line,
                function=function,
                notes=tuple(notes),
            )
        # An eligible instance offered no usable callable; the driver falls through to
        # the module-level names rather than trying a second class.
        return None
    return None


def _module_level_signature(
    module: ModuleSyntax, name: str
) -> ResolvedSignature | UnresolvedEntrypoint | None:
    found = module.module_level_callable(name)
    if found is None:
        return None
    if found.opaque is not None:
        return UnresolvedEntrypoint(
            reason="entrypoint_is_imported_or_aliased",
            detail=f"module-level {name} is bound outside this file",
        )
    if found.function is not None:
        function = found.function
        return ResolvedSignature(
            callable_name=function.name,
            file_path=module.path,
            line=function.line,
            function=function,
        )
    klass = found.constructor
    if klass is None:
        return UnresolvedEntrypoint(reason="entrypoint_resolution_ambiguous", detail=name)
    init = klass.methods.get("__init__")
    if init is None:
        return UnresolvedEntrypoint(
            reason="entrypoint_is_class_without_local_init",
            detail=f"{name} resolves to class {klass.name}",
        )
    return ResolvedSignature(
        callable_name=f"{klass.name}.__init__",
        file_path=module.path,
        line=init.line,
        function=init,
    )


def _resolve_interface_entrypoint(
    interface_row: InterfaceRow, source: PythonTreeSource
) -> ResolvedEntrypoint | UnresolvedEntrypoint:
    name = interface_row.function_name or "main"
    module = source.syntax(interface_row.module_path)
    if module is None:
        return UnresolvedEntrypoint(
            reason="interface_module_outside_tree",
            detail=f"{interface_row.module_path}:{name} is resolved by importlib at runtime",
        )
    signature = _module_level_signature(module, name)
    if isinstance(signature, UnresolvedEntrypoint):
        return signature
    if signature is not None:
        return ResolvedEntrypoint(signature=signature)
    return UnresolvedEntrypoint(
        reason="interface_entrypoint_unresolved", detail=f"{interface_row.module_path}:{name}"
    )


def resolve_entrypoint(
    module: ModuleSyntax,
    func_name: str,
    source: PythonTreeSource,
    *,
    interface_row: InterfaceRow | None = None,
) -> ResolvedEntrypoint | UnresolvedEntrypoint:
    """Reproduce ``_resolve_callable``/``_resolve_module_callable`` for one script row."""
    if module.parse_error is not None:
        return UnresolvedEntrypoint(reason="module_unparsable", detail=module.parse_error)

    if func_name != "main":
        direct = _module_level_signature(module, func_name)
        if isinstance(direct, UnresolvedEntrypoint):
            return direct
        if direct is not None:
            return ResolvedEntrypoint(signature=direct)
    legacy = _legacy_instance_callable(module, func_name, source)
    if func_name == "main":
        # The driver reaches module-level ``func_name`` after the legacy scan only when
        # ``func_name`` is ``main`` (step 1 skips it, step 3 accepts it).
        direct = _module_level_signature(module, func_name)
        if isinstance(direct, UnresolvedEntrypoint):
            return direct
        if direct is not None:
            return ResolvedEntrypoint(signature=direct)
    if legacy is not None:
        return ResolvedEntrypoint(signature=legacy)

    for name in _MODULE_LEVEL_FALLBACK_NAMES:
        signature = _module_level_signature(module, name)
        if isinstance(signature, UnresolvedEntrypoint):
            return signature
        if signature is not None:
            return ResolvedEntrypoint(signature=signature)

    if interface_row is not None:
        return _resolve_interface_entrypoint(interface_row, source)

    return UnresolvedEntrypoint(
        reason="no_callable_entrypoint",
        detail=f"no entrypoint for function_name={func_name}",
    )


def classify_binding(signature: ResolvedSignature, runtime_keys: frozenset[str]) -> BindingVerdict:
    """Classify one resolved signature against the runtime keyword set."""
    function = signature.function
    notes: list[str] = []
    if function.is_decorated:
        notes.append(f"decorated_by_{function.decorators[0]}")

    missing = function.missing_required_keys(runtime_keys)
    if missing:
        name = missing[0]
        if function.is_positional_only(name):
            notes.append("positional_only_cannot_bind_by_keyword")
        return BindingVerdict(
            classification=f"{CLASS_MISSING_REQUIRED_PARAM}:{name}",
            detail_key=name,
            offending_keys=missing,
            notes=tuple(notes),
        )

    if function.has_var_keyword:
        return BindingVerdict(
            classification=CLASS_ACCEPTS_VAR_KWARGS, detail_key="", notes=tuple(notes)
        )

    unexpected = [key for key in sorted(runtime_keys) if not function.accepts_keyword(key)]
    if unexpected:
        if function.has_var_positional:
            notes.append("has_var_positional")
        return BindingVerdict(
            classification=f"{CLASS_UNEXPECTED_PARAM}:{unexpected[0]}",
            detail_key=unexpected[0],
            offending_keys=tuple(unexpected),
            notes=tuple(notes),
        )
    return BindingVerdict(classification=CLASS_BINDS, detail_key="", notes=tuple(notes))


def _dotted_name_list(function: FunctionSyntax) -> list[str]:
    return [param.name for param in function.parameters]


def normalize_task_parameters(raw: object) -> tuple[frozenset[str], str]:
    """Mirror ``_normalize_parameters``; return the key set or an error reason."""
    if raw is None:
        return frozenset(), ""
    if isinstance(raw, dict):
        return frozenset(str(key) for key in raw), ""
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return frozenset(), ""
        parsed: object = text
        for _ in range(2):
            if isinstance(parsed, dict):
                return frozenset(str(key) for key in parsed), ""
            if not isinstance(parsed, str):
                break
            try:
                parsed = json.loads(parsed)
            except (TypeError, ValueError) as exc:
                return frozenset(), f"parameters_not_json_object_{type(exc).__name__}"
        if isinstance(parsed, dict):
            return frozenset(str(key) for key in parsed), ""
        return frozenset(), "parameters_not_json_object"
    return frozenset(), "parameters_not_json_object"


def driver_safe_default_param_keys(driver_file: Path) -> dict[str, tuple[str, ...]]:
    """Extract ``script_id -> injected default keys`` from the driver source.

    ``_apply_safe_default_parameters`` merges ``safe_defaults[script_id]`` under the
    task parameters, so those keys reach the entrypoint too. They are read from the
    driver with :mod:`ast`, never hard-coded here.
    """
    try:
        tree = ast.parse(driver_file.read_text(encoding="utf-8"), filename=str(driver_file))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return {}
    result: dict[str, tuple[str, ...]] = {}
    for node in ast.walk(tree):
        assign: ast.expr | None = None
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "safe_defaults" for target in node.targets
        ):
            assign = node.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "safe_defaults"
        ):
            assign = node.value
        if not isinstance(assign, ast.Dict):
            continue
        for key, value in zip(assign.keys, assign.values, strict=True):
            if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                continue
            if not isinstance(value, ast.Dict):
                continue
            names = tuple(
                item.value
                for item in value.keys
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            )
            result[key.value] = names
    return result


def audit_task(
    task: TaskRecord,
    source: PythonTreeSource,
    *,
    script_file: Path | None,
    default_keys: Mapping[str, tuple[str, ...]] | None = None,
) -> TaskAuditResult:
    """Classify one armed task end to end."""
    injected = tuple((default_keys or {}).get(task.script_id, ()))
    task_keys, key_error = normalize_task_parameters(task.parameters)
    runtime_keys = frozenset(task_keys | set(injected))
    base = TaskAuditResult(
        task_id=task.task_id,
        script_id=task.script_id,
        entrypoint_name=task.entrypoint_name,
        module_path=task.module_path,
        classification="",
        family="",
        detail_key="",
        runtime_param_keys=tuple(sorted(runtime_keys)),
        task_param_keys=tuple(sorted(task_keys)),
        injected_default_keys=injected,
    )
    if key_error:
        return _with_class(base, CLASS_UNRESOLVABLE, reason=key_error)
    if task.module_path is None:
        return _with_class(base, CLASS_UNRESOLVABLE, reason="module_path_null")
    if script_file is None or not script_file.is_file():
        return _with_class(base, CLASS_SCRIPT_FILE_ABSENT)

    module = source.syntax_of_file(script_file, task.module_path)
    resolved = resolve_entrypoint(
        module,
        task.entrypoint_name,
        source,
        interface_row=task.interface_row,
    )
    if isinstance(resolved, UnresolvedEntrypoint):
        notes = (f"detail={resolved.detail}",) if resolved.detail else ()
        return _with_class(base, CLASS_UNRESOLVABLE, reason=resolved.reason, notes=notes)
    verdict = classify_binding(resolved.signature, runtime_keys)
    family = verdict.classification.split(":", 1)[0]
    return _with_class(
        base,
        family,
        detail_key=verdict.detail_key,
        classification=verdict.classification,
        signature=resolved.signature,
        offending_keys=verdict.offending_keys,
        notes=(*verdict.notes, *resolved.signature.notes),
    )


def _with_class(
    base: TaskAuditResult,
    family: str,
    *,
    detail_key: str = "",
    classification: str = "",
    signature: ResolvedSignature | None = None,
    reason: str = "",
    offending_keys: tuple[str, ...] = (),
    notes: tuple[str, ...] = (),
) -> TaskAuditResult:
    return TaskAuditResult(
        task_id=base.task_id,
        script_id=base.script_id,
        entrypoint_name=base.entrypoint_name,
        module_path=base.module_path,
        classification=classification or family,
        family=family,
        detail_key=detail_key,
        runtime_param_keys=base.runtime_param_keys,
        task_param_keys=base.task_param_keys,
        injected_default_keys=base.injected_default_keys,
        offending_keys=offending_keys,
        signature_file=str(signature.file_path) if signature is not None else "",
        signature_line=signature.line if signature is not None else None,
        signature_callable=signature.callable_name if signature is not None else "",
        reason=reason,
        notes=notes,
    )
