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

#: Highest warehouse revision reachable by a no-server ``--sql`` render that
#: walks the chain from base.
#:
#: Revisions above it are listed in ``_ONLINE_ONLY_REFUSALS`` with the reason
#: each one refuses offline: their ``upgrade()`` reads live server state (the
#: Alembic version table's width and marker rows, the existing table or index
#: shape, protected row counts) and the DDL they emit is *chosen* from that
#: read. Rendering them offline would fabricate an inspection result, so the
#: refusal is the correct behaviour and each revision's own test module pins
#: it. ``0015_source_attempt_gate`` is a special case: its bytes are the
#: byte-frozen v1 reference that ``alembic_source_gate.schema_contract``
#: (`V1_REFERENCE_SHA256`) compares against before adopting a gate schema, so
#: editing it to make one more offline render possible would change the
#: reference itself and 178 source-gate tests would refuse to adopt - the
#: pin is the stronger invariant and it stays. It sits behind 0014 either way.
#: ``test_offline_boundary_is_exactly_the_declared_set`` fails if a revision
#: outside the prefix ever renders offline (the prefix must widen) or if a
#: revision stops rendering offline without declaring itself, so the fence can
#: only grow and can never narrow silently.
OFFLINE_HEAD = "0013_sina_fzb_fanout_groups"

#: revision -> the message its offline refusal must carry.
_ONLINE_ONLY_REFUSALS = {
    "0014_sina_fzb_slot_reconciliation": "MySQL offline SQL generation is unsupported",
    "0015_source_attempt_gate": "source attempt gate revision 0015 supports ",
    "0016_legacy_turnover_nullable": "revision 0016 requires an online connection",
    "0017_sina_capture_discovery_index": "revision 0017 requires a live connection",
    "0018_publication_outbox": "revision 0018 requires a live connection",
    "0019_publication_market_symbol_scope": "revision 0019 requires a live connection",
    "0020_run_ledger_claim_policy": "revision 0020 requires an online warehouse connection",
    "0021_temporal_identity_snapshot": "revision 0021 requires an online warehouse connection",
}


def _chain() -> list[tuple[str, str | None]]:
    """Return ``[(revision, down_revision), ...]`` in file order.

    The chain is walked by ``down_revision`` from base so the order is the
    render order, independent of file naming.
    """
    migrations = _load_migrations()
    by_down = {down: revision for revision, down in migrations.values() if down is not None}
    start = next(
        revision for revision, down in migrations.values() if down is None
    )  # base revision has no parent
    walk, current = [start], by_down.get(start)
    while current is not None:
        walk.append(current)
        current = by_down.get(current)
    assert len(walk) == len(migrations), "the chain must walk every revision exactly once"
    return [(revision, None) for revision in walk]


def _render_upgrade_in_process(revision: str) -> tuple[str | None, str]:
    """Run one revision's ``upgrade()`` under a real offline Alembic context.

    Args:
        revision: The revision id to render.

    Returns:
        ``(None, rendered_sql)`` when the revision renders offline, or
        ``(error_text, None)`` when it refuses.
    """
    from io import StringIO

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    from opendata.core.config import settings

    filename = next(name for name, (rev, _) in _load_migrations().items() if rev == revision)
    migration = _load_migration_modules()[filename]
    buffer = StringIO()
    context = MigrationContext.configure(
        url=settings.data_database_url,
        opts={
            "as_sql": True,
            "output_buffer": buffer,
            "version_table": "alembic_version_data",
            "literal_binds": True,
            "dialect_opts": {"paramstyle": "named"},
        },
    )
    try:
        with Operations.context(context):
            migration.upgrade()
    except RuntimeError as exc:
        return str(exc), None
    return None, buffer.getvalue()


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
        sql = _render(["upgrade", OFFLINE_HEAD, "--sql"])

        for table in (*P0_DWD_TABLES, "dwd_stock_adjust", *P0_ODS_TABLES):
            assert f"CREATE TABLE IF NOT EXISTS `{table}`" in sql
        assert "PARTITION pmax VALUES LESS THAN (MAXVALUE)" in sql
        assert "alembic_version_data" in sql

    def test_offline_render_matches_the_generator(self):
        sql = _render(["upgrade", OFFLINE_HEAD, "--sql"])
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

    def test_adjustment_migration_keeps_0005_legacy_shape_and_adds_affine_fields(self):
        sql = _render(["upgrade", OFFLINE_HEAD, "--sql"])
        create_start = sql.index("CREATE TABLE IF NOT EXISTS `dwd_stock_adjust`")
        create_end = sql.index(";", create_start)
        original_table = sql[create_start:create_end]

        assert "`qfq_factor` double NOT NULL" in original_table
        assert "`hfq_factor` double NOT NULL" in original_table
        assert "PRIMARY KEY (`symbol`, `trade_date`)" in original_table
        assert "PARTITION BY RANGE COLUMNS(`trade_date`)" in original_table
        assert "`qfq_scale`" not in original_table
        assert "`legacy_source`" not in original_table
        for column in (
            "qfq_scale",
            "qfq_offset",
            "hfq_scale",
            "hfq_offset",
            "adjustment_version",
            "legacy_source",
        ):
            assert sql.count(f"ADD COLUMN {column}") == 1
        assert "DROP COLUMN" not in sql

    def test_affine_downgrade_drops_only_new_columns(self):
        sql = _render(
            [
                "downgrade",
                "0007_affine_stock_adjust:0006_dwd_daily_legs",
                "--sql",
            ]
        )

        for column in (
            "qfq_scale",
            "qfq_offset",
            "hfq_scale",
            "hfq_offset",
            "adjustment_version",
            "legacy_source",
        ):
            assert f"DROP COLUMN {column}" in sql
        assert "DROP COLUMN qfq_factor" not in sql
        assert "DROP COLUMN hfq_factor" not in sql

    def test_downgrade_renders_drops(self):
        # Offline downgrades need the explicit range (no DB to read from).
        sql = _render(["downgrade", "0001_ods_dwd_p0:base", "--sql"])

        for table in (*P0_DWD_TABLES, *P0_ODS_TABLES):
            assert f"DROP TABLE IF EXISTS `{table}`" in sql

    def test_offline_boundary_is_exactly_the_declared_set(self):
        """Every revision renders offline or declares the reason it cannot.

        This is what keeps ``OFFLINE_HEAD`` honest. Without it, a bounded
        prefix would silently stop covering revisions, and a revision that
        gained a live-inspection guard would drop out of coverage unnoticed.
        """
        chain = [revision for revision, _ in _chain()]
        renderable, refused = [], {}
        for revision in chain:
            error, _sql = _render_upgrade_in_process(revision)
            if error is None:
                renderable.append(revision)
            else:
                refused[revision] = error

        assert set(refused) == set(_ONLINE_ONLY_REFUSALS), (
            f"offline renderability drifted from the declared set: "
            f"undeclared refusals={sorted(set(refused) - set(_ONLINE_ONLY_REFUSALS))}, "
            f"newly renderable={sorted(set(_ONLINE_ONLY_REFUSALS) - set(refused))}"
        )
        for revision, message in refused.items():
            assert _ONLINE_ONLY_REFUSALS[revision] in message, (
                f"{revision} refused offline with an unexpected message: {message}"
            )

        leading_run = []
        for revision in chain:
            if revision in refused:
                break
            leading_run.append(revision)
        assert leading_run[-1] == OFFLINE_HEAD, (
            f"the chain-from-base offline render must stop at {OFFLINE_HEAD}, got {leading_run[-1]}"
        )
        # 0015 stays byte-frozen as the v1 adoption reference, so its own
        # version guard keeps refusing an offline render; see the pin note above.
        assert "0015_source_attempt_gate" in refused
        assert chain[chain.index(OFFLINE_HEAD) + 1] == "0014_sina_fzb_slot_reconciliation"

    def test_head_is_beyond_the_offline_prefix(self):
        """``upgrade head --sql`` must still stop at the first live-only revision.

        Pins the refusal rather than the render: if someone relaxes that
        fail-closed guard to make ``head`` render, this test fails and points
        at the inspection the revision actually needs.
        """
        completed = subprocess.run(  # noqa: S603  # literal argv, no shell
            [sys.executable, "-m", "alembic", "-c", str(INI), "upgrade", "head", "--sql"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        assert completed.returncode != 0
        assert "MySQL offline SQL generation is unsupported" in completed.stderr
        chain = [revision for revision, _ in _chain()]
        assert chain[chain.index(OFFLINE_HEAD) + 1] == "0014_sina_fzb_slot_reconciliation"


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
            assert "dwd_stock_adjust" in names
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
