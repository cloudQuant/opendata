"""Extract per-model interface facts from the pinned upstream OpenBB tree.

The iteration-2 task ledger fixes 350 provider×model identities. This tool reads
only *declaration shape* from the pinned upstream commit — QueryParams field
names/annotations/defaults (including inherited ones) and the declared output
column names of each fetcher's standard model. It never imports, executes or
copies upstream implementation bodies and emits no upstream prose, so the
zero-source-copy constraint still holds.

Output is a deterministic JSON spec sheet keyed by provider and model name.
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
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

UPSTREAM_COMMIT = "3e071fcc2cd9f891cac6040ae60296dba76dab46"
DEFAULT_UPSTREAM_PATH = Path("/Users/yunjinqi/Documents/new_projects/OpenBB/openbb_platform")


def _unparse(node: ast.AST | None) -> str | None:
    return ast.unparse(node) if node is not None else None


def _annassign_fields(node: ast.ClassDef) -> list[dict[str, Any]]:
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
    return fields


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
                "fields": _annassign_fields(node),
            }
            # A subclass may repeat a name across files; keep the first in
            # deterministic path order and record the ambiguity instead.
            if node.name in index:
                ambiguous.append(node.name)
                continue
            index[node.name] = entry
    return index, ambiguous


def _resolved_fields(class_name: str, index: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Collect annotated fields along the base-class chain, parents first."""
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
                    entry["output_columns"] = [
                        f["name"]
                        for f in _resolved_fields(
                            model_class.split("[", 1)[0].strip().rsplit(".", 1)[-1], index
                        )
                    ]
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


def main(argv: list[str] | None = None) -> int:
    """Write the spec sheet, refusing to read an upstream tree that moved."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", type=Path, default=DEFAULT_UPSTREAM_PATH)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--provider", default=None)
    parser.add_argument("--require-counts", default="32,350", help="providers,models")
    args = parser.parse_args(argv)

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
    for data in spec.values():
        for entry in data["models"].values():
            counts[entry["resolution"]] = counts.get(entry["resolution"], 0) + 1
    print(
        f"OK: providers={len(spec)} models={total} resolutions={counts} "
        f"param_median=see_file -> {args.out}"
    )
    return 0


def _unused(values: Iterable[str]) -> None:  # pragma: no cover
    return None


if __name__ == "__main__":
    raise SystemExit(main())
