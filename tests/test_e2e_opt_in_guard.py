"""C59: the live e2e plane is opt-in, and the unit plane never reaches the warehouse.

Two accidents taught this. C58 ran ``pytest tests/test_partition_maintenance.py -q
--no-cov`` without ``-m "not e2e"`` and really did ``CREATE/DROP TABLE`` on production
``opendata_data``; the census below then showed four more legs doing warehouse DDL
*inside* the gate's own ``not e2e`` plane (``docs/evidence/C59/unguarded-before.txt``).
Protection that depends on someone remembering a flag is not protection, so:

1. :mod:`tests.conftest` skips every ``e2e`` item unless an exact release token is set;
2. no unmarked test may build an engine from ``settings.*database_url*``;
3. every ``pytest`` plane in the Makefile deselects ``e2e`` and none releases it -- the
   gate and the guard key on one marker, so they cannot disagree about the list.

Every face carries its own counterfact. The subprocess legs point both MySQL ports at
``127.0.0.1:1`` (connection refused), so nothing in this file can reach the real server.
"""

import ast
import os
import re
import subprocess
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

import pytest

from tests.conftest import (
    LIVE_E2E_MARKER,
    LIVE_E2E_OPT_IN_ENV,
    LIVE_E2E_OPT_IN_TOKEN,
    live_e2e_allowed,
    pytest_collection_modifyitems,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Resolves and refuses: a released leg must fail before its first statement.
DEAD_DB_ENV = {
    "MYSQL_HOST": "127.0.0.1",
    "MYSQL_PORT": "1",
    "DATA_MYSQL_HOST": "127.0.0.1",
    "DATA_MYSQL_PORT": "1",
}

#: The file whose plain invocation damaged the warehouse in C58: 6 unit legs, 3 live.
HAZARD_FILE = "tests/test_partition_maintenance.py"
HAZARD_UNIT_LEGS = 6
HAZARD_LIVE_LEGS = 3

ENGINE_FUNCS = ("create_engine", "create_async_engine")


class _FakeItem:
    """The two methods the collection hook uses off a real ``pytest.Item``."""

    def __init__(self, nodeid: str, markers: tuple[str, ...]) -> None:
        self.nodeid = nodeid
        self._markers = markers
        self.added: list[object] = []

    def get_closest_marker(self, name: str) -> object | None:
        return object() if name in self._markers else None

    def add_marker(self, marker: object) -> None:
        self.added.append(marker)


def _items() -> list[_FakeItem]:
    return [
        _FakeItem("tests/test_x.py::live", (LIVE_E2E_MARKER,)),
        _FakeItem("tests/test_x.py::unit", ("unit",)),
    ]


def _skipped(item: _FakeItem) -> bool:
    return any(getattr(getattr(mark, "mark", None), "name", "") == "skip" for mark in item.added)


def _reason(item: _FakeItem) -> str:
    return str(item.added[0].mark.kwargs["reason"])  # type: ignore[attr-defined]


def _collect() -> list[_FakeItem]:
    items = _items()
    pytest_collection_modifyitems(items=items)
    return items


# --- 1. the hook: default-deny, and release only on the exact token -------------


def test_the_live_plane_is_denied_by_default(monkeypatch):
    monkeypatch.delenv(LIVE_E2E_OPT_IN_ENV, raising=False)
    live, unit = _collect()
    assert _skipped(live) is True, "an e2e item must skip when nothing released it"
    assert _skipped(unit) is False, "the gate must not touch non-e2e items"


def test_an_exact_token_releases_the_live_plane(monkeypatch):
    monkeypatch.setenv(LIVE_E2E_OPT_IN_ENV, LIVE_E2E_OPT_IN_TOKEN)
    assert live_e2e_allowed() is True
    live, unit = _collect()
    assert _skipped(live) is False
    assert _skipped(unit) is False


#: Values a shell profile or a hurried operator might leave behind. None of them
#: is the token, so none of them may open the live plane.
LOOSE_OPT_INS = ["1", "true", "yes", "", "   ", " allow", f"{LIVE_E2E_OPT_IN_TOKEN}x"]


@pytest.mark.parametrize("value", LOOSE_OPT_INS)
def test_a_loose_opt_in_does_not_release_anything(monkeypatch, value):
    """A truthy variable left in a shell profile is the next way to damage the warehouse."""
    monkeypatch.setenv(LIVE_E2E_OPT_IN_ENV, value)
    assert live_e2e_allowed() is False
    assert _skipped(_collect()[0]) is True


def test_the_skip_reason_names_the_variable_and_the_hazard(monkeypatch):
    monkeypatch.delenv(LIVE_E2E_OPT_IN_ENV, raising=False)
    reason = _reason(_collect()[0])
    assert LIVE_E2E_OPT_IN_ENV in reason
    assert "production warehouse" in reason
    assert "not e2e" in reason, "the reason must point at the plane the gate uses"


# --- 2. a real pytest process, in both directions -----------------------------


def _run(*args: str, released: bool) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, **DEAD_DB_ENV, "PYTHONPATH": str(REPO_ROOT)}
    env.pop(LIVE_E2E_OPT_IN_ENV, None)
    if released:
        env[LIVE_E2E_OPT_IN_ENV] = LIVE_E2E_OPT_IN_TOKEN
    return subprocess.run(  # noqa: S603  # fixed argv, no operator input
        [sys.executable, "-m", "pytest", *args, "-p", "no:cacheprovider", "--no-cov", "-rs"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def _tally(report: str) -> dict[str, int]:
    summary = [
        line
        for line in report.splitlines()
        if line.startswith("====") and re.search(r"(passed|failed|error|skipped)", line)
    ]
    assert summary, report[-1500:]
    return {
        name: int(count)
        for count, name in re.findall(
            r"(\d+) (passed|failed|error|errors|skipped|deselected)", summary[-1]
        )
    }


def _skip_reasons(report: str) -> list[str]:
    """Skip reasons with pytest's ``SKIPPED [n] loc: reason`` aggregation expanded."""
    return [
        match.group(2)
        for line in report.splitlines()
        if (match := re.search(r"SKIPPED \[(\d+)\] (.*)", line))
        for _ in range(int(match.group(1)))
    ]


def test_forggetting_the_marker_filter_skips_instead_of_writing():
    """The C58 command verbatim: no ``-m`` filter, and nothing reaches the database."""
    result = _run(HAZARD_FILE, "-q", released=False)
    assert _tally(result.stdout) == {"passed": HAZARD_UNIT_LEGS, "skipped": HAZARD_LIVE_LEGS}
    assert result.returncode == 0
    reasons = _skip_reasons(result.stdout)
    assert len(reasons) == HAZARD_LIVE_LEGS
    assert all(LIVE_E2E_OPT_IN_ENV in line for line in reasons), reasons


def test_releasing_the_token_actually_opens_the_legs():
    """A released leg reaches the (dead) database instead of skipping for policy."""
    result = _run(HAZARD_FILE, "-q", "-m", LIVE_E2E_MARKER, released=True)
    assert _tally(result.stdout) == {"skipped": HAZARD_LIVE_LEGS, "deselected": HAZARD_UNIT_LEGS}
    reasons = _skip_reasons(result.stdout)
    assert len(reasons) == HAZARD_LIVE_LEGS
    assert all("unreachable" in line for line in reasons), reasons
    assert not any(LIVE_E2E_OPT_IN_ENV in line for line in reasons), reasons


# --- 3. static census: no unmarked leg builds a database engine -----------------


def unmarked_engine_defs(source: str) -> list[str]:
    """Defs that construct an engine from a ``settings.*database_url*`` value unguarded."""
    tree = ast.parse(source)

    def module_marks_e2e() -> bool:
        for node in tree.body:
            if isinstance(node, ast.Assign):
                names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names = [node.target.id]
            else:
                continue
            if "pytestmark" in names and "e2e" in (ast.get_source_segment(source, node) or ""):
                return True
        return False

    def engine_calls(node: ast.AST) -> list[ast.Call]:
        return [
            child
            for child in ast.walk(node)
            if isinstance(child, ast.Call)
            and isinstance(child.func, ast.Name)
            and child.func.id in ENGINE_FUNCS
        ]

    found: list[str] = []

    def segment_of(node: ast.AST) -> str:
        return ast.get_source_segment(source, node) or ""

    def reaches_database(node: ast.AST) -> bool:
        calls = engine_calls(node)
        if not calls:
            return False
        # Either the URL is spelled inside the call, or it reaches the call through
        # a local binding -- both leave the def with a live warehouse engine.
        return any("database_url" in segment_of(call) for call in calls) or (
            "database_url" in segment_of(node)
        )

    def walk(body: Iterable[ast.stmt], marked: bool) -> None:
        for node in body:
            if not isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            here = marked or "e2e" in "".join(
                segment_of(decorator) for decorator in node.decorator_list
            )
            if not here and reaches_database(node):
                found.append(node.name)
                continue  # the offence belongs to this scope; nested defs inherit it
            walk(node.body, here)

    walk(tree.body, module_marks_e2e())
    return found


def test_no_test_outside_the_live_plane_builds_a_database_engine():
    offenders = {
        path.name: hits
        for path in sorted((REPO_ROOT / "tests").glob("test_*.py"))
        if (hits := unmarked_engine_defs(path.read_text(encoding="utf-8")))
    }
    assert offenders == {}, offenders


def test_the_census_names_the_legs_that_caused_it():
    """Regression pin: the 2026-09-28 offenders are marked or moved off the warehouse."""
    source = (REPO_ROOT / "tests/test_freshness.py").read_text(encoding="utf-8")
    assert unmarked_engine_defs(source) == []
    assert "@pytest.mark.e2e\nclass TestPartitionPlanCollector" in source
    assert (
        "settings.data_database_url"
        not in source.split("class TestCheckFreshness")[1].split("class TestAlertMatrix")[0]
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "import pytest\nfrom sqlalchemy import create_engine\n"
            "@pytest.mark.e2e\ndef test_live():\n"
            "    from opendata.core.config import settings\n"
            "    create_engine(settings.data_database_url)\n",
            [],
        ),
        (
            "from sqlalchemy import create_engine\n"
            "def test_unit():\n"
            "    from opendata.core.config import settings\n"
            "    create_engine(settings.data_database_url)\n",
            ["test_unit"],
        ),
        (
            "import pytest\npytestmark = pytest.mark.e2e\n"
            "from sqlalchemy import create_engine\n"
            "class TestLive:\n"
            "    def test_any(self):\n"
            "        from opendata.core.config import settings\n"
            "        create_engine(settings.data_database_url)\n",
            [],
        ),
        (
            "def test_local():\n"
            "    from opendata.core.config import settings\n"
            "    url = settings.data_database_url\n"
            "    from sqlalchemy import create_engine\n"
            "    create_engine(url)\n",
            ["test_local"],
        ),
        (
            "from sqlalchemy import create_engine\n"
            "def test_sqlite_only():\n    create_engine('sqlite://')\n",
            [],
        ),
        (
            "from sqlalchemy import create_engine\n"
            "class TestOuter:\n"
            "    def test_inner(self):\n"
            "        from opendata.core.config import settings\n"
            "        create_engine(settings.data_database_url)\n",
            ["TestOuter"],
        ),
    ],
)
def test_the_census_bites_in_both_directions(source, expected):
    assert unmarked_engine_defs(source) == expected


# --- 4. the gate and the guard key off one list --------------------------------


def makefile_pytest_planes(text: str) -> list[str]:
    """Command lines in the Makefile that invoke pytest."""
    return [
        line.strip()
        for line in text.splitlines()
        if re.search(r"^\s*\S*\s*pytest\s", line) and not line.lstrip().startswith("#")
    ]


def planes_that_do_not_deselect_e2e(lines: Sequence[str]) -> list[str]:
    return [line for line in lines if "not e2e" not in line.replace('"', "").replace("'", "")]


def test_every_makefile_pytest_plane_deselects_the_live_legs():
    planes = makefile_pytest_planes((REPO_ROOT / "Makefile").read_text(encoding="utf-8"))
    assert len(planes) == 2, planes
    assert planes_that_do_not_deselect_e2e(planes) == []


def test_the_gate_never_sets_the_release_token():
    assert LIVE_E2E_OPT_IN_ENV not in (REPO_ROOT / "Makefile").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        (['pytest tests -n 8 -m "not e2e"', 'pytest tests -m "not e2e" --cov-branch'], []),
        (["pytest tests -n 8"], ["pytest tests -n 8"]),
        (['pytest tests -m "e2e"'], ['pytest tests -m "e2e"']),
    ],
)
def test_the_plane_census_bites_in_both_directions(lines, expected):
    assert planes_that_do_not_deselect_e2e(lines) == expected


def test_the_guard_and_the_filter_key_off_the_same_marker():
    assert LIVE_E2E_MARKER == "e2e"
    declared = (REPO_ROOT / "pytest.ini").read_text(encoding="utf-8")
    assert "--strict-markers" in declared
    assert re.search(r"^\s+e2e:", declared, re.MULTILINE), "e2e must stay a declared marker"
