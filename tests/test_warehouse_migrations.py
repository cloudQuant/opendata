"""Warehouse migration tests (A4.1).

Three layers, matching what each can prove without the warehouse:

* structural - the revision graph resolves and has a single head
  (mirrors ``test_alembic_migrations.py`` for the application env);
* offline replay - ``upgrade head --sql`` renders the expected
  CREATE TABLE statements and ``downgrade base --sql`` the DROPs,
  proving the chain is replayable and self-consistent with the DDL
  generator;
* live replay (``e2e``) - the migration actually applies and rolls
  back against the configured warehouse database; skipped when it is
  unreachable.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, pool, text

from opendata.pipeline.ddl import dwd_table_ddl, ods_table_ddl

REPO_ROOT = Path(__file__).resolve().parents[1]
VERSIONS_DIR = REPO_ROOT / "alembic_data" / "versions"
INI = REPO_ROOT / "alembic_data.ini"

P0_DWD_TABLES = (
    "dwd_stock_daily",
    "dwd_stock_action",
    "dwd_financial_statement",
    "dwd_financial_indicator",
    "dwd_index_constituent",
)
P0_ODS_TABLES = ("ods_stock_daily_akshare",)


def _load_migration_modules() -> dict[str, object]:
    """Import every migration module (keyed by filename)."""
    modules: dict[str, object] = {}
    for path in sorted(VERSIONS_DIR.glob("*.py")):
        spec = importlib.util.spec_from_file_location(f"alembic_data_migration_{path.stem}", path)
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module  # op.execute needs a live module scope
        spec.loader.exec_module(module)
        modules[path.name] = module
    return modules


def _load_migrations() -> dict[str, tuple[str | None, str | None]]:
    """Return ``{filename: (revision, down_revision)}`` for every migration."""
    return {
        name: (module.revision, module.down_revision)  # type: ignore[attr-defined]
        for name, module in _load_migration_modules().items()
    }


class TestMigrationGraph:
    def test_versions_directory_is_not_empty(self):
        assert _load_migrations(), f"no migrations found under {VERSIONS_DIR}"

    def test_single_head_with_resolvable_chain(self):
        migrations = _load_migrations()
        revisions = {revision for revision, _ in migrations.values()}
        downs = {down for _, down in migrations.values() if down is not None}

        assert len(revisions - downs) == 1, "the chain must have exactly one head"
        for _, down in migrations.values():
            assert down is None or down in revisions, f"dangling down_revision {down!r}"


def _render(args: list[str]) -> str:
    """Run alembic offline against the ini and return its SQL output."""
    return _render_with(INI, args)


def _render_with(ini: Path, args: list[str]) -> str:
    """Run alembic against an explicit ini and return its stdout.

    Args:
        ini: Alembic config file.
        args: Alembic arguments.

    Returns:
        The command's stdout.
    """
    completed = subprocess.run(  # noqa: S603  # literal argv, no shell
        [sys.executable, "-m", "alembic", "-c", str(ini), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout


class TestOfflineReplay:
    def test_upgrade_renders_every_p0_table(self):
        sql = _render(["upgrade", "head", "--sql"])

        for table in (*P0_DWD_TABLES, *P0_ODS_TABLES):
            assert f"CREATE TABLE IF NOT EXISTS `{table}`" in sql
        assert "PARTITION pmax VALUES LESS THAN (MAXVALUE)" in sql
        assert "alembic_version_data" in sql

    def test_offline_render_matches_the_generator(self):
        sql = _render(["upgrade", "head", "--sql"])
        migration = next(iter(_load_migration_modules().values()))

        # The generator is the single source of truth: the shipped DDL
        # must appear verbatim in the rendered migration.
        assert (
            dwd_table_ddl(
                "stock_daily",
                key=("symbol", "trade_date"),
                partition_key="trade_date",
                start_year=2024,
                years=3,
            )
            in sql
        )
        assert (
            ods_table_ddl(
                "stock_daily",
                "akshare",
                migration._STOCK_DAILY_ODS_COLUMNS,  # type: ignore[attr-defined]
                key=("股票代码", "日期"),
                partition_key="日期",
                start_year=2024,
                years=3,
            )
            in sql
        )

    def test_downgrade_renders_drops(self):
        # Offline downgrades need the explicit range (no DB to read from).
        sql = _render(["downgrade", "0001_ods_dwd_p0:base", "--sql"])

        for table in (*P0_DWD_TABLES, *P0_ODS_TABLES):
            assert f"DROP TABLE IF EXISTS `{table}`" in sql


@pytest.mark.e2e
class TestLiveReplay:
    """The replay runs on a SCRATCH database, never on the warehouse.

    ``downgrade base`` drops every ods/dwd table, so replaying it on
    the configured warehouse destroys whatever data lives there (it
    wiped a 617k-row migration once). The scratch database keeps the
    proof - the chain applies and rolls back - without touching real
    data.
    """

    SCRATCH_DB = "opendata_data_migration_replay"

    @pytest.fixture
    def scratch_ini(self, tmp_path):
        """Create the scratch database and an ini pointing at it."""
        from sqlalchemy.engine import make_url

        from opendata.core.config import settings

        url = make_url(settings.data_database_url)
        try:
            server = create_engine(url.set(database=None), poolclass=pool.NullPool)
            with server.connect() as connection:
                connection.execute(text("SELECT 1"))
                connection.execute(text(f"DROP DATABASE IF EXISTS `{self.SCRATCH_DB}`"))
                connection.execute(
                    text(f"CREATE DATABASE `{self.SCRATCH_DB}` CHARACTER SET utf8mb4")
                )
        except Exception as exc:  # any connection failure means skip
            pytest.skip(f"warehouse database unreachable: {type(exc).__name__}")
        scratch_url = url.set(database=self.SCRATCH_DB).render_as_string(hide_password=False)
        # Rewrite the ini's URL: drop the commented template line and put
        # the scratch URL right under [alembic] so it actually applies.
        lines = [
            line
            for line in INI.read_text(encoding="utf-8").splitlines()
            if not line.strip().lstrip("#").strip().startswith("sqlalchemy.url")
        ]
        for index, line in enumerate(lines):
            if line.strip() == "[alembic]":
                lines.insert(index + 1, f"sqlalchemy.url = {scratch_url}")
                break
        ini = tmp_path / "alembic_data_scratch.ini"
        ini.write_text("\n".join(lines) + "\n", encoding="utf-8")
        yield ini, server, url
        with server.begin() as connection:
            connection.execute(text(f"DROP DATABASE IF EXISTS `{self.SCRATCH_DB}`"))
        server.dispose()

    def test_upgrade_then_downgrade_on_a_scratch_database(self, scratch_ini):
        ini, server, url = scratch_ini
        scratch = create_engine(url.set(database=self.SCRATCH_DB), poolclass=pool.NullPool)
        try:
            _render_with(ini, ["upgrade", "head"])
            names = set(inspect(scratch).get_table_names())
            assert set(P0_DWD_TABLES) <= names
            assert set(P0_ODS_TABLES) <= names
            with scratch.connect() as connection:
                version = connection.execute(
                    text("SELECT version_num FROM alembic_version_data")
                ).scalar()
            revisions = {revision for revision, _ in _load_migrations().values()}
            downs = {down for _, down in _load_migrations().values() if down is not None}
            assert version in revisions - downs  # the single head of the chain

            _render_with(ini, ["downgrade", "base"])
            assert not (set(P0_DWD_TABLES) & set(inspect(scratch).get_table_names()))
        finally:
            scratch.dispose()
