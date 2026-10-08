"""C36: the ported tree's import closure must be covered by declared dependencies.

Why this file exists
-------------------
The AKShare vendor facade is lazy: importing the app or its metadata must not run
the 325 migrated source modules. Reading a selected export must load its real
implementation and its module-scope dependencies. ``curl_cffi`` is used by one
such export and remains part of that measured dependency closure.

The device
---------
Run ``import opendata.main`` in a child process and keep the module names that
land in ``sys.modules``. For every *first-party* file that executed, read its
module-scope ``import`` statements: those third-party names are import-time
requirements of the shipped runtime. A requirement is covered when its
distribution is reachable from ``[project.dependencies] + [web] + [dev]``,
resolved through installation metadata rather than string matching, because
``mini-racer`` ships ``py_mini_racer`` and ``beautifulsoup4`` ships ``bs4``.

Nothing is inferred from the developer's convenience: a library pandas merely
*prefers* (bottleneck, orjson, zstandard) is not a requirement here, because no
first-party file imports it.
"""

from __future__ import annotations

import ast
import importlib.metadata
import json
import os
import subprocess
import sys
from functools import cache, lru_cache
from importlib.metadata import packages_distributions
from pathlib import Path
from typing import TYPE_CHECKING

import tomllib

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
APP_IMPORT = "opendata.main"
VENDOR_PACKAGE = "opendata.data.providers.akshare._vendor"
VENDOR_ROOT = REPO_ROOT / "opendata" / "data" / "providers" / "akshare" / "_vendor"
VENDOR_LOCK = VENDOR_ROOT / "upstream.lock"
LEGACY_VENDOR_PACKAGE = "opendata_http"
# Must match the install step in .github/workflows/ci.yml (`pip install -e ".[web,dev]"`).
CI_EXTRAS = ("web", "dev")
RUNTIME_ROOTS = ("opendata", "opendata_fuyao", "opendata_client")

_CLOSURE_DUMP = """
import sys
before = set(sys.modules)
__import__(sys.argv[1])
print("|".join(sorted(set(sys.modules) - before)))
"""

_EXPORT_CLOSURE_DUMP = """
import importlib, sys
before = set(sys.modules)
facade = importlib.import_module(sys.argv[1])
getattr(facade, sys.argv[2])
print("|".join(sorted(set(sys.modules) - before)))
"""

_BLOCK_PROBE = """
import sys
import importlib

blocked = {m.split(".")[0] for m in sys.argv[2].split(",") if m}


class _Blocker:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in blocked:
            raise ModuleNotFoundError("blocked for test: " + name, name=name)
        return None


sys.meta_path.insert(0, _Blocker())
try:
    module = importlib.import_module(sys.argv[1])
    if len(sys.argv) > 3:
        getattr(module, sys.argv[3])
except Exception as exc:
    print("BLOCKED_SO_IMPORT_FAILED " + type(exc).__name__)
    sys.exit(1)
print("IMPORT_STILL_OK")
"""


def _run_child(
    code: str, import_target: str = APP_IMPORT, *args: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603  # nosec B603  # literal argv, shell is never used
        [sys.executable, "-c", code, import_target, *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
    )


@lru_cache(maxsize=8)
def import_closure(import_target: str = APP_IMPORT, export: str | None = None) -> frozenset[str]:
    """Module names loaded by an app import or one selected lazy vendor export."""
    code = _CLOSURE_DUMP if export is None else _EXPORT_CLOSURE_DUMP
    args = () if export is None else (export,)
    result = _run_child(code, import_target, *args)
    assert result.returncode == 0, (
        f"{import_target} import does not complete in a clean child:\n{result.stderr[-700:]}"
    )
    return frozenset(x for x in result.stdout.strip().split("|") if x)


def locked_vendor_python_files() -> tuple[Path, ...]:
    """Resolve the full Python inventory from the preserved lock, fail on missing paths."""
    lock = json.loads(VENDOR_LOCK.read_text(encoding="utf-8"))
    files = tuple(
        VENDOR_ROOT / Path(entry["path"])
        for entry in lock["files"]
        if Path(entry["path"]).suffix == ".py"
    )
    missing = [path.relative_to(VENDOR_ROOT).as_posix() for path in files if not path.is_file()]
    assert not missing, f"Python files recorded in upstream.lock are missing: {missing}"
    return files


def locked_vendor_modules() -> frozenset[str]:
    """The complete Python module inventory recorded by the preserved upstream lock."""
    modules: set[str] = set()
    for source_path in locked_vendor_python_files():
        relative = source_path.relative_to(VENDOR_ROOT)
        parts = (
            relative.parent.parts
            if relative.name == "__init__.py"
            else relative.with_suffix("").parts
        )
        suffix = ".".join(parts)
        modules.add(VENDOR_PACKAGE if not suffix else f"{VENDOR_PACKAGE}.{suffix}")
    return frozenset(modules)


def first_party_files_that_executed(
    import_target: str = APP_IMPORT, export: str | None = None
) -> list[str]:
    """Closure members that are our own files - the ones whose imports we own."""
    return sorted(
        m
        for m in import_closure(import_target, export)
        if any(m == r or m.startswith(f"{r}.") for r in RUNTIME_ROOTS)
    )


def module_to_path(module: str) -> Path | None:
    parts = module.split(".")
    base = REPO_ROOT.joinpath(*parts)
    for path in (base.with_name(base.name + ".py"), base / "__init__.py"):
        if path.is_file():
            return path
    return None


_IMPORT_FAILURE_NAMES = frozenset(
    {"ImportError", "ModuleNotFoundError", "Exception", "BaseException"}
)

# Matched by class name because `ast.TryStar` only exists on 3.11+, while the type
# checker is pinned to the project's minimum version.
_TRY_NODE_NAMES = frozenset({"Try", "TryStar"})


def _handler_names(node: ast.expr | None) -> set[str]:
    if node is None:
        return set()
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, ast.Attribute):
        return {node.attr}
    if isinstance(node, ast.Tuple):
        return {n for elt in node.elts for n in _handler_names(elt)}
    if isinstance(node, ast.BinOp):  # `except ImportError | OSError:`
        return _handler_names(node.left) | _handler_names(node.right)
    return set()


def _swallows_import_failure(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:  # bare `except:`
        return True
    return bool(_handler_names(handler.type) & _IMPORT_FAILURE_NAMES)


@cache
def _scan(path: Path) -> tuple[frozenset[str], frozenset[str]]:
    """(required, guarded) top-level names for one file's module scope.

    "Guarded" means the import sits under a module-level ``try`` whose handler would
    swallow the ``ModuleNotFoundError`` - an optional accelerator or an integration
    the deployment may not have. Those are not import-time requirements, and a first
    version of this guard cried wolf about exactly that: ``sentry_sdk``
    (``opendata/main.py:84`` under ``except ImportError``) and ``akqmt``
    (the old eager vendor facade) both raised "declare this" for libraries
    the shipped app never needs to boot.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - defensive
        return frozenset(), frozenset()
    required: set[str] = set()
    guarded: set[str] = set()
    stack: list[tuple[ast.stmt, bool]] = [(child, False) for child in tree.body]
    while stack:
        node, in_guard = stack.pop()
        bucket = guarded if in_guard else required
        if isinstance(node, ast.Import):
            bucket.update(a.name.split(".")[0] for a in node.names if a.name)
        elif isinstance(node, ast.ImportFrom):
            if not node.level and node.module:
                bucket.add(node.module.split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        elif type(node).__name__ in _TRY_NODE_NAMES:
            # `finally` runs inside the statement, so a swallowing handler guards it too.
            handlers: Iterable[ast.ExceptHandler] = getattr(node, "handlers", ())
            swallows = in_guard or any(_swallows_import_failure(h) for h in handlers)
            for field in ("body", "handlers", "orelse", "finalbody"):
                stack.extend((child, swallows) for child in getattr(node, field, []))
        elif isinstance(node, (ast.If, ast.While, ast.For, ast.With, ast.ExceptHandler)):
            for field in ("body", "handlers", "orelse", "finalbody"):
                stack.extend((child, in_guard) for child in getattr(node, field, []))
    return frozenset(required), frozenset(guarded - required)


def module_scope_imports(path: Path) -> set[str]:
    """Top-level names the file imports where a failure would propagate out.

    The distinction is the whole point: ``akqmt`` and ``playwright`` appear in the
    vendor tree but stay outside a selected export's module-scope closure unless that
    export actually imports them.
    """
    return set(_scan(path)[0])


def guarded_imports(path: Path) -> set[str]:
    """Top-level names whose ``ImportError`` the file itself swallows."""
    return set(_scan(path)[1])


def _normalize(dist: str) -> str:
    return "-".join(part.lower() for part in dist.replace("_", "-").split("-"))


def _spec_name(spec: str) -> str:
    """``limits (>=2.3)`` and ``uvicorn[standard]>=0.32`` each name one distribution."""
    head = spec.split(";")[0].strip()
    token = head.split()[0] if head.split() else ""
    for sep in ("[", "===", "==", ">=", "<=", "!=", "~=", ">", "<"):
        token = token.split(sep)[0]
    return _normalize(token)


@lru_cache(maxsize=1)
def declared_distributions() -> frozenset[str]:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = data["project"]
    specs: list[str] = list(project.get("dependencies", []))
    extras = project.get("optional-dependencies", {})
    for extra in CI_EXTRAS:
        assert extra in extras, f"pyproject has no `{extra}` extra, but CI installs it"
        specs += list(extras[extra])
    return frozenset(_spec_name(spec) for spec in specs)


def _requirement_name(req: str) -> str | None:
    """Distribution name of a PEP 508 requirement, or None when pip would skip it
    because it is gated on an extra we did not request."""
    head, _, marker = req.partition(";")
    name = _spec_name(head)
    if not name:
        return None
    marker = marker.replace("'", '"')
    if "extra ==" in marker:
        requested = {f'extra == "{extra}"' for extra in CI_EXTRAS}
        return name if any(token in marker for token in requested) else None
    return name


@lru_cache(maxsize=1)
def reachable_distributions() -> frozenset[str]:
    """What `pip install -e ".[web,dev]"` puts on the path: the declared seeds plus
    their transitive requirements, walked through installed distribution metadata."""
    seen = set(declared_distributions())
    frontier = list(seen)
    while frontier:
        name = frontier.pop()
        try:
            requires = importlib.metadata.distribution(name).requires or []
        except importlib.metadata.PackageNotFoundError:
            continue
        for req in requires:
            child = _requirement_name(req)
            if child and child not in seen:
                seen.add(child)
                frontier.append(child)
    return frozenset(seen)


def provider_distributions(module: str) -> frozenset[str]:
    """Distribution names that install ``module``, read from environment metadata."""
    return frozenset(_normalize(d) for d in packages_distributions().get(module, ()))


def _third_party(names: Iterable[str]) -> frozenset[str]:
    stdlib = frozenset(sys.stdlib_module_names)
    return frozenset(
        m
        for m in set(names)
        if m not in stdlib and not any(m == r or m.startswith(f"{r}.") for r in RUNTIME_ROOTS)
    )


def _imported_names(
    scan: Callable[[Path], set[str]], import_target: str, export: str | None
) -> frozenset[str]:
    out: set[str] = set()
    for module in first_party_files_that_executed(import_target, export):
        path = module_to_path(module)
        if path is not None:
            out |= scan(path)
    return _third_party(out)


def runtime_requirements(
    import_target: str = APP_IMPORT, export: str | None = None
) -> frozenset[str]:
    """Third-party imports for the selected app or lazy export closure."""
    return _imported_names(module_scope_imports, import_target, export)


def guarded_runtime_imports(
    import_target: str = APP_IMPORT, export: str | None = None
) -> frozenset[str]:
    """Third-party top-levels imported under a handler that swallows the failure."""
    return _imported_names(guarded_imports, import_target, export)


def uncovered_requirements(modules: Iterable[str]) -> list[str]:
    reachable = reachable_distributions()
    return [m for m in sorted(set(modules)) if not (provider_distributions(m) & reachable)]


def blocking_breaks_app_import(
    modules: Iterable[str], import_target: str = APP_IMPORT, export: str | None = None
) -> bool:
    args = (
        (",".join(sorted(set(modules))),)
        if export is None
        else (
            ",".join(sorted(set(modules))),
            export,
        )
    )
    return _run_child(_BLOCK_PROBE, import_target, *args).returncode != 0


def test_the_facade_is_lazy_and_exports_on_demand() -> None:
    """Use the lock inventory and fresh children to verify lazy loading directly."""
    locked = locked_vendor_modules()
    assert len(locked) == 325, (
        f"the upstream lock must retain 325 Python modules, got {len(locked)}"
    )

    metadata_closure = import_closure(VENDOR_PACKAGE, "__version__")
    assert locked & metadata_closure == {VENDOR_PACKAGE, f"{VENDOR_PACKAGE}._version"}
    assert not any(
        module == LEGACY_VENDOR_PACKAGE or module.startswith(f"{LEGACY_VENDOR_PACKAGE}.")
        for module in metadata_closure
    ), "the obsolete top-level package must not be restored as a shim"

    selected_closure = import_closure(VENDOR_PACKAGE, "futures_hog_core")
    selected_modules = locked & selected_closure
    implementation = f"{VENDOR_PACKAGE}.futures_derivative.futures_hog"
    assert implementation in selected_modules
    assert len(selected_modules) < len(locked), "one export must not load the full vendor tree"
    assert not any(
        module == "akshare" or module.startswith("akshare.") for module in selected_closure
    ), "the selected export must resolve inside the migrated namespace"
    assert not any(
        module == "openbb" or module.startswith("openbb.") for module in selected_closure
    ), "the selected export must not load the external OpenBB runtime"

    missing = uncovered_requirements(runtime_requirements(VENDOR_PACKAGE, "futures_hog_core"))
    assert not missing, f"the selected export has undeclared module-scope imports: {missing}"


def test_every_module_scope_import_is_reachable_from_the_declared_set() -> None:
    missing = uncovered_requirements(runtime_requirements())
    install = f'pip install -e ".[{",".join(CI_EXTRAS)}]"'
    assert not missing, (
        f"`import {APP_IMPORT}` needs {missing}, which `{install}` does not provide. "
        "Declare them in [project.dependencies] or move the import behind a lazy boundary."
    )

    # Lazy exports are absent from the cold app's module closure, so preserve the
    # former whole-tree requirement census by scanning every Python file named by
    # the lock rather than narrowing coverage to files that happened to load.
    vendor_imports: set[str] = set()
    for source_path in locked_vendor_python_files():
        vendor_imports |= module_scope_imports(source_path)
    vendor_missing = uncovered_requirements(_third_party(vendor_imports))
    assert not vendor_missing, (
        f"undeclared module-scope imports in locked vendor files: {vendor_missing}"
    )


def test_guarded_imports_are_optional_and_stay_out_of_the_requirement_set() -> None:
    """A swallowed ``ImportError`` is an optional integration, not an undeclared dependency.

    ``sentry_sdk`` remains a guarded app import. ``akqmt`` is exposed only through a
    lazy optional facade entry, so it is outside the cold app-import AST closure. The
    block probe below checks guarded app imports empirically.
    """
    guarded = guarded_runtime_imports()
    assert "sentry_sdk" in guarded, (
        f"the walker no longer classifies the optional imports as guarded: {sorted(guarded)}"
    )
    assert "sentry_sdk" not in runtime_requirements()
    assert "akqmt" not in runtime_requirements()
    assert "curl_cffi" not in guarded, (
        "curl_cffi is a genuine import-time requirement and must not be classed as guarded"
    )
    assert not blocking_breaks_app_import(guarded), (
        "hiding every guarded import broke the app, so one of them is really required: "
        f"{sorted(guarded)}"
    )


def test_curl_cffi_regression_stays_declared() -> None:
    """The lazy HK valuation export still declares its module-scope curl_cffi use."""
    selected_requirements = runtime_requirements(VENDOR_PACKAGE, "stock_hk_valuation_baidu")
    assert "curl_cffi" in selected_requirements
    assert "curl-cffi" in reachable_distributions(), (
        "curl-cffi must be declared or transitively reachable"
    )
    assert "curl-cffi" in provider_distributions("curl_cffi"), (
        "curl_cffi must resolve through metadata"
    )


def test_the_block_probe_device_has_teeth_in_both_directions() -> None:
    """Hiding an export dependency fails on access while cold app import stays lazy.

    Without the negative control this guard could pass vacuously: a device that reports
    "failed" for every module would call noise a defect, and one that reports "fine" for
    every module would hide the C36 defect again.
    """
    assert not blocking_breaks_app_import({"curl_cffi"}), (
        "blocking the lazy curl_cffi export must not break `import opendata.main`"
    )
    assert blocking_breaks_app_import({"curl_cffi"}, VENDOR_PACKAGE, "stock_hk_valuation_baidu"), (
        "blocking curl_cffi did not break the selected export access - the probe is dead"
    )
    assert "openpyxl" not in runtime_requirements(), "openpyxl is expected to stay a lazy import"
    assert not blocking_breaks_app_import({"openpyxl"}), (
        "blocking the lazy openpyxl broke `import opendata.main` - it is not optional after all"
    )


def test_metadata_resolution_beats_name_matching() -> None:
    """`mini-racer` ships `py_mini_racer`: string matching would call it undeclared.

    An ad-hoc AST-vs-pyproject name comparison in C36 flagged six libraries as
    undeclared; bs4 -> beautifulsoup4 and py_mini_racer -> mini-racer were already
    declared, while akqmt and playwright remain behind lazy entry points.
    """
    for module in ("py_mini_racer", "bs4", "yaml", "dateutil"):
        assert provider_distributions(module), (
            f"{module} should resolve to an installed distribution"
        )
    assert provider_distributions("bs4") & reachable_distributions()
    assert provider_distributions("py_mini_racer") & reachable_distributions()
    assert provider_distributions("dateutil") & reachable_distributions()
