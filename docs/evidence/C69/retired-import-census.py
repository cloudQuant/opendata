"""Which record archives really import a retired top-level name, decided by AST.

A plain grep over ``docs/evidence`` counts every occurrence of ``opendata_fuyao`` and cannot tell an
import statement from the same text sitting inside a fixture source string that a script only hands
to ``ast.parse``. That difference decides whether a record is re-runnable, so the census reads each
file's syntax tree and reports the two populations apart, including the string-literal hits it
declines to count as imports.

Each retired import is then resolved through the repository's own layout authority
(``scripts/quality/source_layout.historical_identity``) rather than a second mapping table written
here. If the authority refuses a name, that refusal is printed as the finding: this script has no
competing claim about where the file went, and an unmapped historical name is a real gap.

Offline: parsing, stat, and importing the authority module only. No provider package is imported, no
request is made, no database is touched.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import sys
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[3]
#: The retired-name probe below is only a real zero if the repository root is on the search path,
#: since that is how every one of these records was run. Without it, "not resolvable" would be an
#: artefact of the path rather than a fact about the tree.
sys.path[:0] = [str(ROOT), str(ROOT / "scripts" / "quality")]

from source_layout import (  # noqa: E402 - after the path setup above
    VENDOR_ROOT,
    SourceLayoutError,
    historical_identity,
)

RETIRED = frozenset({"opendata_fuyao", "opendata_http"})


def head_commit() -> str:
    """Read the checked-in commit identity out of ``.git`` rather than running a git process.

    A record script that shells out adds an execution surface to a face that is meant to be pure
    parsing, and ``packed-refs`` would need a second code path anyway; the loose ref is what a
    developer branch holds.
    """
    raw = (ROOT / ".git" / "HEAD").read_text(encoding="utf-8").strip()
    if not raw.startswith("ref: "):
        return raw[:12]
    ref = (ROOT / ".git" / raw[5:]).read_text(encoding="utf-8").strip()
    return f"{raw[5:]} {ref[:12]}"


class Site(NamedTuple):
    """One retired import statement: where it is, what it names, and the files it could open."""

    lineno: int
    statement: str
    candidates: tuple[str, ...]

    @property
    def rendered(self) -> str:
        """Print the site with the layout authority's answer beside it.

        The authority is asked about every candidate the statement could denote and the first answer
        it accepts is published; if it refuses them all, its own refusal is the finding, because
        that means no canonical file in the current tree carries this historical name.
        """
        last = ""
        for historical in self.candidates:
            try:
                target = historical_identity(historical)
            except SourceLayoutError as exc:
                last = str(exc)
                continue
            path = ROOT / target
            present = path.is_file() or path.is_dir()
            state = "on-disk" if present else "MISSING"
            return f"{self.statement} -> {target} [{state}]"
        return f"{self.statement} -> REFUSED({last})"


def _root(module: str) -> str:
    """Return the top-level package of a dotted module name."""
    return module.split(".", 1)[0]


def _historical_files(module: str, alias: str) -> tuple[str, ...]:
    """List the historical files one retired import statement could denote.

    ``import opendata_fuyao.errors`` reads ``opendata_fuyao/errors.py``. ``from opendata_fuyao
    import endpoints`` denotes the same file, the alias being a submodule of the root; when the
    alias is a symbol rather than a file, only the root package itself is denoted, and the
    authority's own answer decides which reading is published.
    """
    parts = module.split(".")
    if len(parts) > 1:
        return ("/".join(parts) + ".py",)
    root_only = (module,)
    if alias and alias != "*":
        return ("/".join([parts[0], alias]) + ".py", *root_only)
    return root_only


def scan(path: Path) -> tuple[list[Site], list[str], str | None]:
    """Return ``(import_sites, string_sites, parse_error)`` for one archive script."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as exc:
        return [], [], f"SyntaxError line {exc.lineno}"
    sites: list[Site] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            sites += [
                Site(node.lineno, f"import {alias.name}", _historical_files(alias.name, ""))
                for alias in node.names
                if _root(alias.name) in RETIRED
            ]
        elif isinstance(node, ast.ImportFrom) and node.module and _root(node.module) in RETIRED:
            alias = node.names[0].name if node.names else "*"
            sites.append(
                Site(
                    node.lineno,
                    f"from {node.module} import {alias}",
                    _historical_files(node.module, alias),
                )
            )
    strings: list[str] = []
    if not sites:
        strings.extend(
            f"{node.lineno}:{name} (inside a string constant)"
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            for name in sorted(RETIRED)
            if name in node.value
        )
    return sites, strings, None


def census(files: list[Path]) -> tuple[list[tuple[Path, list[Site]]], list[tuple[Path, list[str]]]]:
    """Split the archive scripts into real imports and string-only mentions."""
    with_imports: list[tuple[Path, list[Site]]] = []
    string_only: list[tuple[Path, list[str]]] = []
    for path in files:
        sites, strings, error = scan(path)
        if error:
            print(f"  UNPARSEABLE {path.relative_to(ROOT)} {error}")
        if sites:
            with_imports.append((path, sites))
        elif strings:
            string_only.append((path, strings))
    return with_imports, string_only


def _retired_name_face() -> None:
    """Show that the retired names denote nothing at all now, so a record importing one cannot run.

    ``find_spec`` resolves a top-level name without executing it — there is no parent package to
    import, so neither the retired name nor ``opendata`` runs a module body. The canonical targets
    are checked on the filesystem instead of by resolution, because resolving a dotted submodule
    would import the very provider packages the layout cases are about.
    """
    print("\n--- retired-name availability face (find_spec only; nothing executed) ---")
    live = importlib.util.find_spec("opendata") is not None
    print(f"  positive control: opendata resolvable={live} (repo root is on the search path)")
    for name in sorted(RETIRED):
        resolvable = importlib.util.find_spec(name) is not None
        print(f"  {name}: resolvable={resolvable} (importing it now raises ModuleNotFoundError)")
    for relative in ("opendata/data/providers/ths", VENDOR_ROOT):
        directory = ROOT / relative
        files = len(list(directory.rglob("*.py"))) if directory.is_dir() else 0
        print(f"  canonical {relative}: is_dir={directory.is_dir()} python_files={files}")
    print("  this census imports no retired name; it parses the records that do")


def main() -> int:
    """Print the census: two populations, and the authority's answer for every import."""
    here = Path(__file__).resolve()
    files = sorted(p for p in ROOT.glob("docs/evidence/**/*.py") if p.resolve() != here)
    with_imports, string_only = census(files)
    print(f"python={sys.version.split()[0]} scanned_python_files={len(files)}")
    print(f"files_with_real_imports={len(with_imports)}")
    sites = sum(len(group) for _, group in with_imports)
    refused = 0
    missing = 0
    for path, group in with_imports:
        for site in group:
            rendered = site.rendered
            refused += rendered.count("REFUSED(")
            missing += rendered.count("[MISSING]")
            print(f"  {path.relative_to(ROOT)}:{site.lineno} {rendered}")
    print(f"import_sites={sites} refused_by_authority={refused} target_missing_on_disk={missing}")
    print(
        f"files_with_string_only_hits={len(string_only)}"
        "  (mentions inside a string constant, NOT imports: those scripts run as they stand)"
    )
    for path, notes in string_only:
        for note in notes:
            print(f"  {path.relative_to(ROOT)} {note}")
    _retired_name_face()
    print(f"HEAD={head_commit()}")
    print(f"self_sha256={hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
