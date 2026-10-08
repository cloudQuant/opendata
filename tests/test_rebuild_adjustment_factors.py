"""The guarded factor rebuild CLI defaults to an isolated, no-write dry run."""

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    func,
    insert,
    select,
)

from opendata.data.domains import ods_table
from scripts.ops import rebuild_adjustment_factors

DAY1 = date(2026, 9, 23)
EVENT_DAY = date(2026, 9, 24)
SYMBOL = "600519.SH"


def _local_sqlite_url(path: Path) -> str:
    """Build a fixture URL from a filesystem path; reject connection strings."""
    if not isinstance(path, Path):
        raise ValueError("local warehouse fixtures require a filesystem Path")
    return f"sqlite:///{path}"


def _local_sqlite_engine(path: Path):
    """Open only the file-backed SQLite database owned by this fixture."""
    return create_engine(_local_sqlite_url(path))


def _warehouse(sqlite_path: Path):
    metadata = MetaData()
    daily = Table(
        ods_table("stock_daily", "ths"),
        metadata,
        Column("thscode", String(64), primary_key=True),
        Column("trade_date", Date, primary_key=True),
        Column("adjusted", String(16), nullable=False),
        Column("close_price", Float, nullable=False),
    )
    actions = Table(
        ods_table("stock_action", "ths"),
        metadata,
        Column("thscode", String(64), primary_key=True),
        Column("ex_date", Date, primary_key=True),
        Column("dividend_per_share", Float),
        Column("per_share_bonus", Float),
        Column("allotment_ratio", Float),
        Column("allotment_price", Float),
        Column("event_key", String(64)),
    )
    factors = Table(
        "dwd_stock_adjust",
        metadata,
        Column("symbol", String(64), primary_key=True),
        Column("trade_date", Date, primary_key=True),
        Column("qfq_factor", Float, nullable=False),
        Column("hfq_factor", Float, nullable=False),
        Column("source", String(64), nullable=False),
        Column("legacy_source", String(64)),
        Column("qfq_scale", Float),
        Column("qfq_offset", Float),
        Column("hfq_scale", Float),
        Column("hfq_offset", Float),
        Column("adjustment_version", String(32)),
        Column("_merged_at", DateTime),
        Column("_diff_flag", Integer),
        Column("_as_of", Date),
    )
    engine = _local_sqlite_engine(sqlite_path)
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            insert(daily),
            [
                {
                    "thscode": SYMBOL,
                    "trade_date": DAY1,
                    "adjusted": "none",
                    "close_price": 10.0,
                },
                {
                    "thscode": SYMBOL,
                    "trade_date": EVENT_DAY,
                    "adjusted": "none",
                    "close_price": 12.0,
                },
            ],
        )
        connection.execute(
            insert(actions),
            {
                "thscode": SYMBOL,
                "ex_date": EVENT_DAY,
                "dividend_per_share": 1.0,
                "per_share_bonus": 0.0,
                "allotment_ratio": 0.0,
                "allotment_price": 0.0,
                "event_key": "cash-1",
            },
        )
        connection.execute(
            insert(factors),
            {
                "symbol": "600519",
                "trade_date": DAY1,
                "qfq_factor": 0.91,
                "hfq_factor": 1.0,
                "source": "historical-import",
                "legacy_source": None,
            },
        )
    return engine, factors


def _settings_with_sqlite_url(sqlite_url: str) -> SimpleNamespace:
    """Supply only the local fixture URL expected by the CLI configuration seam."""
    return SimpleNamespace(data_database_url=sqlite_url)


def test_cli_default_dry_run_reads_registered_ods_and_preserves_existing_rows(
    monkeypatch, capsys, tmp_path
):
    sqlite_path = tmp_path / "factor-cli.db"
    sqlite_url = _local_sqlite_url(sqlite_path)
    engine, factors = _warehouse(sqlite_path)
    monkeypatch.setattr(
        "sqlalchemy.create_engine", lambda url: engine if url == sqlite_url else None
    )
    monkeypatch.setattr(
        "opendata.core.config.get_settings",
        lambda: _settings_with_sqlite_url(sqlite_url),
    )

    try:
        assert (
            rebuild_adjustment_factors.main(
                ["--start", DAY1.isoformat(), "--end", EVENT_DAY.isoformat()]
            )
            == 0
        )
        output = capsys.readouterr().out
        check_engine = _local_sqlite_engine(sqlite_path)
        with check_engine.connect() as connection:
            count = connection.execute(select(func.count()).select_from(factors)).scalar_one()
            row = (
                connection.execute(select(factors).where(factors.c.trade_date == DAY1))
                .mappings()
                .one()
            )
        check_engine.dispose()
    finally:
        engine.dispose()

    assert "mode=dry-run" in output
    assert "symbols=1" in output
    assert "planned=2" in output
    assert "written=0" in output
    assert "events_outside_basis=0" in output
    assert "legacy_rows_preserved=1" in output
    assert "postgresql://" not in output
    assert "fixture-secret" not in output
    assert count == 1
    assert row["qfq_factor"] == pytest.approx(0.91)
    assert row["legacy_source"] is None


def test_local_sqlite_helper_rejects_connection_string_before_engine_creation(monkeypatch):
    def unexpected_engine(_url):
        pytest.fail("the local fixture must reject connection strings before engine creation")

    monkeypatch.setattr("tests.test_rebuild_adjustment_factors.create_engine", unexpected_engine)
    with pytest.raises(ValueError, match="filesystem Path"):
        _local_sqlite_engine("postgresql://fixture.invalid/production")  # type: ignore[arg-type]


def test_cli_rejects_reversed_window_before_opening_a_database(monkeypatch):
    def no_database(*_args, **_kwargs):
        pytest.fail("date validation must happen before opening an engine")

    monkeypatch.setattr("sqlalchemy.create_engine", no_database)

    with pytest.raises(SystemExit) as exc_info:
        rebuild_adjustment_factors.main(
            ["--start", EVENT_DAY.isoformat(), "--end", DAY1.isoformat()]
        )

    assert exc_info.value.code == 2


@pytest.mark.parametrize("failure_stage", ["settings", "engine"])
def test_cli_sanitizes_configuration_and_engine_construction_errors(
    monkeypatch, capsys, failure_stage
):
    secret = "postgresql://fixture-user:fixture-secret@db.invalid/factors"
    if failure_stage == "settings":

        def fail_settings():
            raise RuntimeError(secret)

        monkeypatch.setattr("opendata.core.config.get_settings", fail_settings)
    else:
        monkeypatch.setattr(
            "opendata.core.config.get_settings",
            lambda: SimpleNamespace(data_database_url=secret),
        )

        def fail_engine(_url):
            raise RuntimeError(secret)

        monkeypatch.setattr("sqlalchemy.create_engine", fail_engine)

    assert rebuild_adjustment_factors.main([]) == 1
    output = capsys.readouterr().err
    assert "factor rebuild failed: RuntimeError" in output
    assert secret not in output
