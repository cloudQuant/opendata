#!/usr/bin/env python3
"""Census stale offline fakes: test doubles patched onto a transport seam.

The repository answers provider I/O offline by monkeypatching a seam function
whose declaration carries a ``transport`` parameter (the offline witness seam).
When the product started forwarding ``transport=`` through that seam, every fake
written to the older signature began raising ``TypeError: ... got an unexpected
keyword argument 'transport'``. This census finds those sites from the ASTs of
both sides, so the repair list is a measured population, not a hand-typed guess.

Resolution is module-qualified at both ends, because name-only matching invents
findings:
  * Many providers define ``_http_get`` / ``get_json`` and only some declare
    ``transport``; a same-named function in another package is a different seam.
  * A product call is charged to the (file, name) its callee resolves to, so a seam
    counts as forwarding ``transport=`` only when a call site really hands it there.
  * Tests patch the name where it is *called*: ``yield_curve.py`` re-exports
    ``fetch_yield_curve`` from ``_series_client.py``, so import bindings (including
    the package-relative ``from . import _client`` form) are followed one hop.

Every shape the tests use is read: both ``setattr`` spellings, function-local imports,
``module.submodule`` targets, and fakes produced by a factory call. A site whose fake
already declares ``transport`` or ``**kwargs`` is released before target resolution, so
an unresolvable target never turns a safe fake into a finding.

Faces:
  population  - transport seams, seams the product forwards ``transport=`` to,
                setattr sites, sites naming a seam attribute
  judgment    - stale sites, plus the two honest unknowns (unresolved target / fake)

``--arm`` seeds every shape in both states (stale and widened) and requires the flagged
set to equal exactly the stale seeds.
"""

from __future__ import annotations

import argparse
import ast
import tempfile
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[4]
SEAM_ARG = "transport"
SEAM_KEY = tuple[str, str]


def _label(path: Path) -> str:
    try:
        return path.relative_to(REPO).as_posix()
    except ValueError:
        return str(path)


def _dotted_for(relative: str) -> str:
    return relative[: -len(".py")].replace("/", ".")


def _package_dotted(relative: str) -> str:
    dotted = _dotted_for(relative)
    if dotted.endswith(".__init__"):
        return dotted[: -len(".__init__")]
    return dotted.rsplit(".", 1)[0] if "." in dotted else dotted


def _declared_params(args: ast.arguments) -> tuple[list[str], bool]:
    declared = [arg.arg for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]]
    return declared, args.kwarg is not None


def _module_index(product_dir: Path) -> dict[str, str]:
    """Map dotted module paths to repository-relative file names."""
    index: dict[str, str] = {}
    for path in sorted(product_dir.rglob("*.py")):
        relative = _label(path)
        dotted = _dotted_for(relative)
        index[dotted] = relative
        if path.name == "__init__.py":
            index[dotted[: -len(".__init__")]] = relative
    return index


def _resolve_dotted(dotted: str, modules: dict[str, str]) -> str | None:
    parts = [part for part in dotted.split(".") if part]
    for cut in range(len(parts), 0, -1):
        candidate = ".".join(parts[:cut])
        if candidate in modules:
            return modules[candidate]
    return None


def _bind_tree(
    tree: ast.Module, current_file: str, modules: dict[str, str]
) -> tuple[dict[str, tuple[str, str]], dict[str, str]]:
    """Return (name -> (file, attribute), name -> module file) visible in one module."""
    visible: dict[str, tuple[str, str]] = {}
    module_visible: dict[str, str] = {}
    package = _package_dotted(current_file)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef | ast.ClassDef):
            visible.setdefault(node.name, (current_file, node.name))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if isinstance(node, ast.Import):
                    origin_dotted = alias.name
                else:
                    base = "" if node.level == 0 else package
                    for _ in range(node.level - 1):
                        base = base.rsplit(".", 1)[0] if "." in base else base
                    suffix = f"{node.module}.{alias.name}" if node.module else alias.name
                    origin_dotted = f"{base}.{suffix}" if base else suffix
                origin_file = _resolve_dotted(origin_dotted, modules)
                if origin_file is None:
                    continue
                local_name = alias.asname or alias.name.split(".")[0]
                origin_attr = alias.name.split(".")[-1]
                visible[local_name] = (origin_file, origin_attr)
                if origin_dotted in modules:
                    module_visible[local_name] = origin_file
    return visible, module_visible


def product_seams(product_dir: Path, modules: dict[str, str]) -> tuple[
    dict[SEAM_KEY, dict[str, Any]],
    dict[str, dict[str, tuple[str, str]]],
    dict[str, dict[str, str]],
]:
    """Return (transport seams, per-module name origins, per-module module origins)."""
    seams: dict[SEAM_KEY, dict[str, Any]] = {}
    visible_all: dict[str, dict[str, tuple[str, str]]] = {}
    module_all: dict[str, dict[str, str]] = {}
    product_files = sorted(product_dir.rglob("*.py"))
    for path in product_files:
        relative = _label(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        visible, module_visible = _bind_tree(tree, relative, modules)
        visible_all[relative] = visible
        module_all[relative] = module_visible
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
                declared, var_kw = _declared_params(node.args)
                if SEAM_ARG in declared:
                    seams[(relative, node.name)] = {"declared": declared, "var_kw": var_kw}

    def receiver_module(expr: ast.expr, current_file: str) -> str | None:
        """Return the module file a call receiver denotes, or None."""
        if isinstance(expr, ast.Name):
            return module_all.get(current_file, {}).get(expr.id)
        if isinstance(expr, ast.Attribute):
            base = receiver_module(expr.value, current_file)
            if base is not None:
                chained = f"{_dotted_for(base)}.{expr.attr}"
                if chained in modules:
                    return modules[chained]
                hop = module_all.get(base, {}).get(expr.attr)
                if hop is not None:
                    return hop
            chained = f"{_package_dotted(current_file)}.{expr.attr}"
            return modules.get(chained)
        return None

    def callee(func: ast.expr, current_file: str) -> SEAM_KEY | None:
        """Resolve a call's callee to (defining file, attribute name)."""
        if isinstance(func, ast.Name):
            return visible_all.get(current_file, {}).get(func.id)
        if isinstance(func, ast.Attribute):
            mod = receiver_module(func.value, current_file)
            if mod is None:
                return None
            hop = visible_all.get(mod, {}).get(func.attr)
            return hop or (mod, func.attr)
        return None

    forwarded: dict[SEAM_KEY, int] = {}
    for path in product_files:
        relative = _label(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not any(kw.arg == SEAM_ARG for kw in node.keywords):
                continue
            key = callee(node.func, relative)
            if key in seams:
                forwarded[key] = forwarded.get(key, 0) + 1
    for key, entry in seams.items():
        entry["forwarded"] = forwarded.get(key, 0)
    return seams, visible_all, module_all


def _import_maps(
    tree: ast.Module, modules: dict[str, str]
) -> tuple[dict[str, str], dict[str, str]]:
    """Return (name -> module file, name -> (file, attr)) for a test module."""
    module_visible: dict[str, str] = {}
    visible: dict[str, tuple[str, str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        prefix = node.module if isinstance(node, ast.ImportFrom) else ""
        for alias in node.names:
            local = alias.asname or alias.name.split(".")[0]
            dotted = alias.name if isinstance(node, ast.Import) else f"{prefix}.{alias.name}"
            relative = _resolve_dotted(dotted, modules)
            if relative is None:
                continue
            visible[local] = (relative, alias.name.split(".")[-1])
            if dotted in modules:
                module_visible[local] = relative
    return module_visible, visible


def _resolve_target(
    target: ast.expr,
    module_visible: dict[str, str],
    visible: dict[str, tuple[str, str]],
    modules: dict[str, str],
    product_modules: dict[str, dict[str, str]],
) -> str | None:
    """Return the module file a setattr target names, or None when unresolved."""
    if isinstance(target, ast.Constant) and isinstance(target.value, str):
        return _resolve_dotted(target.value, modules)
    if isinstance(target, ast.Name):
        resolved = module_visible.get(target.id)
        if resolved is not None:
            return resolved
        hop = visible.get(target.id)
        return hop[0] if hop else None
    if isinstance(target, ast.Attribute):
        base = _resolve_target(
            target.value, module_visible, visible, modules, product_modules
        )
        if base is None:
            return None
        chained = f"{_dotted_for(base)}.{target.attr}"
        if chained in modules:
            return modules[chained]
        hop = product_modules.get(base, {}).get(target.attr)
        return hop
    return None


def _attr_name(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return node.id
    return None


def _inner_returned(factory: ast.FunctionDef) -> ast.FunctionDef | None:
    """Return the callable a factory hands back (by returned name, or its only inner def)."""
    inner = [
        node
        for node in ast.walk(factory)
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node is not factory
    ]
    returned = {
        node.value.id
        for node in ast.walk(factory)
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Name)
    }
    for candidate in inner:
        if candidate.name in returned:
            return candidate
    return inner[0] if len(inner) == 1 else None


def _bindings(scope: ast.AST) -> dict[str, ast.AST]:
    """Return the callables bound directly in one scope, without entering nested ones."""
    defs: dict[str, ast.AST] = {}
    stack: list[ast.AST] = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef | ast.ClassDef):
            defs.setdefault(node.name, node)
            continue
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            defs.setdefault(node.targets[0].id, node.value)
        stack.extend(ast.iter_child_nodes(node))
    return defs


def _file_scopes(tree: ast.Module) -> tuple[dict[int, ast.AST], dict[int, dict[str, ast.AST]]]:
    """Return (parent link by node id, name bindings by scope node id) for one module."""
    parents: dict[int, ast.AST] = {}
    bindings: dict[int, dict[str, ast.AST]] = {id(tree): _bindings(tree)}
    stack: list[ast.AST] = [tree]
    while stack:
        node = stack.pop()
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
            if isinstance(child, ast.AsyncFunctionDef | ast.FunctionDef):
                bindings[id(child)] = _bindings(child)
            stack.append(child)
    return parents, bindings


def _lookup(
    site: ast.AST, parents: dict[int, ast.AST], bindings: dict[int, dict[str, ast.AST]]
) -> dict[str, ast.AST]:
    """Merge the bindings visible at ``site`` so the innermost definition wins.

    A test that defines its own ``fake_get`` shadows the module-level one of the same
    name; resolving by bare name in whole-file order would judge the wrong signature.
    """
    scopes: list[dict[str, ast.AST]] = []
    current: ast.AST | None = site
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if id(current) in bindings:
            scopes.append(bindings[id(current)])
        current = parents.get(id(current))
    merged: dict[str, ast.AST] = {}
    for scope in reversed(scopes):
        merged.update(scope)
    return merged


def _resolve_fake(
    fake: ast.expr, local_defs: dict[str, ast.AST]
) -> tuple[str, list[str], bool, bool, ast.expr | None]:
    """Return (label, declared params, takes **kwargs, resolvable, node to widen)."""
    if isinstance(fake, ast.Lambda):
        declared, var_kw = _declared_params(fake.args)
        return "lambda", declared, var_kw, True, fake
    if isinstance(fake, ast.Name):
        node = local_defs.get(fake.id)
        if isinstance(node, ast.Lambda):
            declared, var_kw = _declared_params(node.args)
            return fake.id, declared, var_kw, True, node
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            declared, var_kw = _declared_params(node.args)
            return fake.id, declared, var_kw, True, node
        return f"{fake.id}?", [], False, False, None
    if isinstance(fake, ast.Call) and isinstance(fake.func, ast.Name):
        node = local_defs.get(fake.func.id)
        if isinstance(node, ast.FunctionDef):
            produced = _inner_returned(node)
            if produced is not None:
                declared, var_kw = _declared_params(produced.args)
                return (
                    f"{fake.func.id}()->{produced.name}",
                    declared,
                    var_kw,
                    True,
                    produced,
                )
    return "expr", [], False, False, None


def seam_for_patch(
    module_file: str,
    attr: str,
    seams: dict[SEAM_KEY, dict[str, Any]],
    visible: dict[str, dict[str, tuple[str, str]]],
) -> SEAM_KEY | None:
    """Return the seam key a patched attribute names, following one import hop."""
    if (module_file, attr) in seams:
        return (module_file, attr)
    origin = visible.get(module_file, {}).get(attr)
    return origin if origin in seams else None


def scan_tests(
    tests_dir: Path,
    seams: dict[SEAM_KEY, dict[str, Any]],
    visible: dict[str, dict[str, tuple[str, str]]],
    module_visible: dict[str, dict[str, str]],
) -> dict[str, Any]:
    """Classify every setattr site against the transport seam population."""
    modules = _module_index(REPO / "opendata")
    seam_names = {name for _file, name in seams}
    stale: list[dict[str, Any]] = []
    unknown_target: list[dict[str, Any]] = []
    unknown_fake: list[dict[str, Any]] = []
    total = 0
    named = 0
    for path in sorted(tests_dir.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        test_module_visible, test_visible = _import_maps(tree, modules)
        parents, bindings = _file_scopes(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr != "setattr" or len(node.args) < 2:
                continue
            total += 1
            if len(node.args) >= 3:
                attr = _attr_name(node.args[1])
                fake = node.args[2]
            else:
                value = node.args[0]
                tail = (
                    value.value.rsplit(".", 1)
                    if isinstance(value, ast.Constant) and isinstance(value.value, str)
                    else []
                )
                attr = tail[-1] if len(tail) == 2 and tail[0] else None
                fake = node.args[1]
            if attr is None or attr not in seam_names:
                continue
            named += 1
            label, declared, var_kw, resolvable, widen = _resolve_fake(
                fake, _lookup(node, parents, bindings)
            )
            if resolvable and (var_kw or SEAM_ARG in declared):
                continue
            module_file = _resolve_target(
                node.args[0], test_module_visible, test_visible, modules, module_visible
            )
            site = {
                "file": _label(path),
                "line": node.lineno,
                "attr": attr,
                "fake": label,
                "declared": declared,
            }
            if module_file is None:
                unknown_target.append(site)
                continue
            key = seam_for_patch(module_file, attr, seams, visible)
            if key is None:
                continue
            site["seam"] = f"{key[0]}::{key[1]}"
            if not resolvable:
                unknown_fake.append(site)
                continue
            if seams[key]["forwarded"] == 0:
                continue
            site["widen"] = widen
            site["path"] = path
            stale.append(site)
    return {
        "stale": stale,
        "unknown_target": unknown_target,
        "unknown_fake": unknown_fake,
        "total": total,
        "named": named,
    }


_SEED_IMPORTS = {
    "dotted": "",
    "module": "import opendata.data.providers.fred.models._client as fred_client",
    "chain": "import opendata.data.providers.fred.models._paged as fred_paged",
    "local_import": "",
    "reexport": "import opendata.data.providers.ecb.models.yield_curve as yc_module",
    "factory": "import opendata.data.providers.fred.models._client as fred_client",
    "shadow": "",
}

_SEED_CALLS = {
    "dotted": (
        '    monkeypatch.setattr(\n        "opendata.data.providers.fred.models._client'
        '._http_get", fake)'
    ),
    "module": '    monkeypatch.setattr(fred_client, "_http_get", fake)',
    "chain": '    monkeypatch.setattr(fred_paged._client, "_http_get", fake)',
    "local_import": (
        "    from opendata.data.providers.fred.models import _client as late_client\n"
        '    monkeypatch.setattr(late_client, "_http_get", fake)'
    ),
    "reexport": '    monkeypatch.setattr(yc_module, "fetch_yield_curve", fake)',
    "factory": '    monkeypatch.setattr(fred_client, "_http_get", make_fake())',
    "shadow": (
        '    monkeypatch.setattr(\n        "opendata.data.providers.fred.models._client'
        '._http_get", fake)'
    ),
}

ARM_SHAPES = ("dotted", "module", "chain", "local_import", "reexport", "factory", "shadow")


def _shadow_source(stale: bool) -> str:
    """Seed a test-local fake that shadows a module-level fake of the same name.

    Both halves matter: the stale seed has a tolerant module-level twin (a whole-file
    name lookup would release the stale local one), and the widened seed has a tight
    twin (the same lookup would charge a clean site as stale).
    """
    outer = "url, params, timeout, **kwargs" if stale else "url, params, timeout"
    inner = "url, params, timeout" if stale else "url, params, timeout, **kwargs"
    return (
        f"def fake({outer}):\n    return 200, 'module'\n\n\n"
        "def test_case(monkeypatch):\n"
        f"    def fake({inner}):\n        return 200, 'local'\n"
        '    monkeypatch.setattr(\n        "opendata.data.providers.fred.models._client'
        '._http_get", fake)\n'
    )


def _arm_source(shape: str, stale: bool) -> str:
    """Return one seed module: a fake of the given shape, stale or widened."""
    if shape == "shadow":
        return _shadow_source(stale)
    if shape == "reexport":
        params = "query, timeout=None" if stale else "query, timeout=None, **kwargs"
        statement = "return ()"
    else:
        params = "url, params, timeout" if stale else "url, params, timeout, **kwargs"
        statement = "return 200, 'ok'"
    chunks = [_SEED_IMPORTS[shape]]
    if shape == "factory":
        chunks.append(
            f"\n\ndef make_fake():\n    def inner({params}):\n        {statement}\n"
            "    return inner"
        )
    else:
        chunks.append(f"\n\ndef fake({params}):\n    {statement}")
    chunks.append(f"\n\ndef test_case(monkeypatch):\n{_SEED_CALLS[shape]}")
    return "".join(chunks).lstrip("\n") + "\n"


def _arm() -> int:
    seeds = [(shape, stale) for shape in ARM_SHAPES for stale in (True, False)]
    with tempfile.TemporaryDirectory(prefix="fake-census-arm-") as tmp:
        tests = Path(tmp)
        for shape, stale in seeds:
            state = "stale" if stale else "widened"
            source = _arm_source(shape, stale)
            ast.parse(source)
            tests.joinpath(f"test_zz_{shape}_{state}.py").write_text(source, encoding="utf-8")
        modules = _module_index(REPO / "opendata")
        seams, visible, module_visible = product_seams(REPO / "opendata", modules)
        result = scan_tests(tests, seams, visible, module_visible)
        flagged = sorted(Path(site["file"]).name for site in result["stale"])
    expected = sorted(f"test_zz_{shape}_stale.py" for shape in ARM_SHAPES)
    bites = (
        flagged == expected
        and result["total"] == len(seeds)
        and result["named"] == len(seeds)
        and not result["unknown_target"]
        and not result["unknown_fake"]
    )
    print(
        f"arm: seeded={len(seeds)} scanned={result['total']} seam_named={result['named']} "
        f"unknown_target={len(result['unknown_target'])} unknown_fake={len(result['unknown_fake'])}"
    )
    print(f"arm: flagged={flagged}")
    print(f"arm: expected={expected} -> {'PASS' if bites else 'FAIL'}")
    return 0 if bites else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", action="store_true", help="run the falsification arm only")
    parser.add_argument("--tests-dir", default=str(REPO / "tests"))
    args = parser.parse_args(argv)
    if args.arm:
        return _arm()

    modules = _module_index(REPO / "opendata")
    seams, visible, module_visible = product_seams(REPO / "opendata", modules)
    result = scan_tests(Path(args.tests_dir), seams, visible, module_visible)
    stale = result["stale"]
    files = {site["file"] for site in stale}
    print(
        f"population: seams={len(seams)} seam_names={len({name for _f, name in seams})} "
        f"seams_forwarded={sum(1 for e in seams.values() if e['forwarded'])}"
    )
    print(f"population: setattr_sites={result['total']} seam_named_sites={result['named']}")
    print(
        f"face: stale_sites={len(stale)} in_files={len(files)} "
        f"unknown_target={len(result['unknown_target'])} unknown_fake={len(result['unknown_fake'])}"
    )
    for site in stale:
        print(
            f"stale: {site['file']}:{site['line']} seam={site['seam']} fake={site['fake']} "
            f"declared={site['declared']}"
        )
    for site in result["unknown_target"] + result["unknown_fake"]:
        print(
            f"unknown: {site['file']}:{site['line']} attr={site['attr']} fake={site['fake']} "
            f"declared={site['declared']} seam={site.get('seam')}"
        )
    by_file: dict[str, int] = {}
    for site in stale:
        by_file[site["file"]] = by_file.get(site["file"], 0) + 1
    for name, count in sorted(by_file.items(), key=lambda item: (-item[1], item[0])):
        print(f"by_file: {name} stale={count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
