"""The P0-domain integration surface AC-16 条目 7 has to be able to name (C51).

C51 measured the referent of 「在未安装 akshare/openbb 的干净环境中 P0 域集成测试通过」and found
none: ``pytest.ini`` registers an ``integration`` marker that **no test applied**, and
``pytest -m "integration and not e2e"`` collected ``0 of 3358`` items in both the development
interpreter and a clean venv (``docs/evidence/C51/census-before.txt``,
``clean-env-integration-run.txt``). A criterion that cannot name its own test set is not
verifiable, so 条目 7 could only ever be narrated.

These asserts keep the named set honest after the wiring: the marker stays registered, the modules
that carry it stay the reviewed ones, every P0 domain from the A4.1 migration stays covered by at
least one of them, and no marked module can be emptied out by marking all of its tests ``e2e`` -
which would leave the selector passing on a set of zero.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: The selector the clean-environment run uses: integration-shaped, and never the warehouse.
SELECTOR = "integration and not e2e"

#: A4.1 - the migration that enumerates the P0 warehouse tables.
P0_MIGRATION = "alembic_data/versions/20260923-0001_ods_dwd_p0.py"

#: Modules that drive a P0 domain across module boundaries against a real engine or the app.
#: Adding one is a review step: it changes what "the P0 domain integration tests" means.
P0_INTEGRATION_MODULES = frozenset(
    {
        "tests/test_data_query_api.py",
        "tests/test_dwd_merge.py",
        "tests/test_index_constituent_asof.py",
        "tests/test_ods_writer.py",
        "tests/test_p0_providers.py",
        "tests/test_pipeline_run_api.py",
        "tests/test_pipeline_runner.py",
    }
)

#: Classes marked inside otherwise unit-shaped modules (the file's own docstring says "Unit").
P0_INTEGRATION_CLASSES = frozenset({"tests/test_pipeline_jobs.py::TestFullCheckExecutor"})


def _parse(rel: str) -> ast.Module:
    """Parse a repo file as Python.

    Args:
        rel: Repository-relative path.

    Returns:
        The module AST.
    """
    return ast.parse((ROOT / rel).read_text(encoding="utf-8"), filename=rel)


def _mark_names(node: ast.AST) -> set[str]:
    """Collect every ``pytest.mark.<name>`` appearing in a subtree.

    Args:
        node: Module, class or function node.

    Returns:
        The marker names found.
    """
    found: set[str] = set()
    for child in ast.walk(node):
        if (
            isinstance(child, ast.Attribute)
            and isinstance(child.value, ast.Attribute)
            and isinstance(child.value.value, ast.Name)
            and child.value.value.id == "pytest"
            and child.value.attr == "mark"
        ):
            found.add(child.attr)
    return found


def _marked_units() -> list[tuple[str, ast.AST]]:
    """Every integration-marked unit the selector reaches right now.

    A unit is a module carrying a module-level ``pytestmark`` or a decorated test class, so a
    class marked inside an otherwise unit-shaped module is measured over that class alone.

    Returns:
        ``(identifier, node)`` pairs, sorted, where the identifier is a module path or
        ``module::Class``.
    """
    units: list[tuple[str, ast.AST]] = []
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        rel = str(path.relative_to(ROOT))
        tree = _parse(rel)
        for node in tree.body:
            if (
                isinstance(node, ast.Assign)
                and "integration" in _mark_names(node)
                and any(getattr(t, "id", "") == "pytestmark" for t in node.targets)
            ):
                units.append((rel, tree))
            if isinstance(node, ast.ClassDef) and "integration" in _mark_names(node):
                units.append((f"{rel}::{node.name}", node))
    return units


def _p0_domains() -> list[str]:
    """The P0 domains as the A4.1 migration names them.

    Returns:
        Keys of its ``_DWD_TABLES`` mapping, in file order.
    """
    tables = next(
        (
            stmt
            for stmt in _parse(P0_MIGRATION).body
            if isinstance(stmt, ast.Assign)
            and any(getattr(t, "id", "") == "_DWD_TABLES" for t in stmt.targets)
        ),
        None,
    )
    assert tables is not None, f"{P0_MIGRATION} no longer assigns _DWD_TABLES"
    assert isinstance(tables.value, ast.Dict)
    return [ast.unparse(key) for key in tables.value.keys if key is not None]


def test_integration_marker_is_registered_under_strict_markers() -> None:
    """The selector only means something while ``--strict-markers`` and the registration agree."""
    ini = (ROOT / "pytest.ini").read_text(encoding="utf-8")
    assert "--strict-markers" in ini, "without strict markers an unregistered marker is a warning"
    block = ini.split("markers =", 1)[1]
    registered = {
        line.strip().split(":")[0]
        for line in block.splitlines()
        if line.startswith((" ", "\t")) and line.strip()
    }
    assert "integration" in registered, f"integration is not registered: {sorted(registered)}"
    assert "e2e" in registered, f"e2e is not registered: {sorted(registered)}"


def test_the_marked_set_is_the_reviewed_set() -> None:
    """A criterion's denominator may not drift by accident in either direction."""
    assert {rel for rel, _ in _marked_units()} == {*P0_INTEGRATION_MODULES, *P0_INTEGRATION_CLASSES}


def test_every_p0_domain_is_reached_by_the_selection() -> None:
    """The set is *P0 domain* integration tests, so each P0 domain must appear in it.

    Measured over the live marks rather than the allow-list above: removing a ``pytestmark``
    shrinks the selection, and this is the assert that says out loud what that costs.
    """
    text = "".join(
        (ROOT / rel.split("::")[0]).read_text(encoding="utf-8") for rel, _ in _marked_units()
    )
    missing = [domain for domain in _p0_domains() if domain.strip("'\"") not in text]
    assert not missing, f"P0 domains no marked test names: {missing}"


def test_no_marked_unit_is_all_e2e() -> None:
    """A marked unit whose every test is also ``e2e`` would empty the selector in silence."""
    units = _marked_units()
    assert units, "no module or class carries the integration marker any more"
    for rel, node in units:
        functions = [
            child
            for child in ast.walk(node)
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
            and child.name.startswith("test_")
        ]
        assert functions, f"{rel} has no test function left to select"
        selected = [fn for fn in functions if "e2e" not in _mark_names(fn)]
        assert selected, f"{rel} is marked integration but every test is e2e"
