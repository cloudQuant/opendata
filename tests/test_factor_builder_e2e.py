"""Factor builder end-to-end tests (AC-11 adjust, design D10).

Seeds a deterministic pair of ods tables (closes + one dividend event),
runs the builder into ``dwd_stock_adjust`` and verifies the REST
``adjust=qfq`` path serves the synthesized series - the same route a
consumer hits. The akshare-official qfq cross-check stays a separate,
network-gated item (AC-2/AC-11 comparison, pending eastmoney access).
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine, pool, text

PROBE = "ADJUST_PROBE"
EVENT_DAY = date(2026, 9, 22)
DAY1 = date(2026, 9, 21)

_DAILY_COLUMNS = (
    "(`thscode`, `trade_date`, `date_ms`, `currency`, `interval`, `adjusted`, "
    "`open_price`, `high_price`, `low_price`, `close_price`, `volume`, `turnover`, "
    "`_source`, `_fetched_at`, `_batch_id`)"
)
_ACTION_COLUMNS = (
    "(`thscode`, `ticker`, `ex_date`, `ex_date_ms`, `event_key`, "
    "`dividend_per_share`, `per_share_bonus`, `allotment_ratio`, `allotment_price`, `currency`, "
    "`_source`, `_fetched_at`, `_batch_id`)"
)


def _ensure_factor_table(engine) -> None:
    """Create the stock-adjust dwd table when it is missing.

    Tests that predate the migration may have left the version marker
    applied while the table itself is gone (a fixture dropped it); the
    fixture heals the schema instead of failing the whole run.
    """
    from opendata.pipeline.ddl import dwd_table_ddl

    with engine.begin() as connection:
        connection.execute(
            text(
                dwd_table_ddl(
                    "stock_adjust",
                    key=("symbol", "trade_date"),
                    partition_key="trade_date",
                )
            )
        )


@pytest.mark.e2e
class TestFactorBuilder:
    @pytest.fixture
    def warehouse(self):
        import subprocess
        import sys
        from pathlib import Path

        from opendata.core.config import settings

        engine = create_engine(settings.data_database_url, poolclass=pool.NullPool)
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except Exception as exc:
            pytest.skip(f"warehouse database unreachable: {type(exc).__name__}")
        subprocess.run(  # literal argv, no shell
            [sys.executable, "-m", "alembic", "-c", "alembic_data.ini", "upgrade", "head"],
            cwd=Path(__file__).resolve().parents[1],
            check=True,
            capture_output=True,
        )
        _ensure_factor_table(engine)
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE IF EXISTS `test_daily`"))
            connection.execute(text("DROP TABLE IF EXISTS `test_action`"))
            connection.execute(
                text("DELETE FROM `dwd_stock_adjust` WHERE `symbol` = :probe"),
                {"probe": PROBE},
            )
            connection.execute(text("CREATE TABLE `test_daily` LIKE `ods_stock_daily_ths`"))
            connection.execute(text("CREATE TABLE `test_action` LIKE `ods_stock_action_ths`"))
            # Day 1: close 10.0, unadjusted. Day 2 (the ex-date): close 12.0.
            connection.execute(
                text(
                    "INSERT INTO `test_daily` " + _DAILY_COLUMNS + " VALUES "  # noqa: S608  # literal DDL
                    "(:probe, :d1, 1, 'CNY', '1d', 'none', 10.0, 10.0, 10.0, 10.0, 100, 1000, "
                    "'ths', NOW(), 'b-1'), "
                    "(:probe, :d2, 2, 'CNY', '1d', 'none', 12.0, 12.0, 12.0, 12.0, 100, 1200, "
                    "'ths', NOW(), 'b-1')"
                ),
                {"probe": PROBE, "d1": DAY1, "d2": EVENT_DAY},
            )
            # A 1.0 CNY dividend effective on day 2.
            connection.execute(
                text(
                    "INSERT INTO `test_action` " + _ACTION_COLUMNS + " VALUES "  # noqa: S608  # literal DDL
                    "(:probe, :probe, :d2, 2, 'event', 1.0, 0.0, 0.0, 0.0, 'CNY', "
                    "'ths', NOW(), 'b-2')"
                ),
                {"probe": PROBE, "d2": EVENT_DAY},
            )
        yield engine
        with engine.begin() as connection:
            for table in ("test_daily", "test_action"):
                connection.execute(text(f"DROP TABLE IF EXISTS `{table}`"))
            connection.execute(
                text("DELETE FROM `dwd_stock_adjust` WHERE `symbol` = :probe"),
                {"probe": PROBE},
            )
        engine.dispose()

    def test_builder_cumulates_and_writes_the_factor_rows(self, warehouse):
        from opendata.pipeline.factor_builder import FactorBuilder

        builder = FactorBuilder(
            warehouse,
            daily_table="test_daily",
            action_table="test_action",
        )
        result = builder.build()

        assert result.symbols == 1
        assert result.rows_written == 2
        with warehouse.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT `symbol`, `trade_date`, `qfq_factor`, `hfq_factor` "
                    "FROM `dwd_stock_adjust` WHERE `symbol` = :probe ORDER BY `trade_date`"
                ),
                {"probe": PROBE},
            ).fetchall()
        assert len(rows) == 2
        day1, day2 = rows
        assert day1[1] == DAY1 and day2[1] == EVENT_DAY
        assert day1[2] == pytest.approx(0.9)  # 9/10: qfq before the dividend
        assert day1[3] == pytest.approx(1.0)  # hfq anchor
        assert day2[2] == pytest.approx(1.0)  # qfq anchor on the ex-date
        assert day2[3] == pytest.approx(10.0 / 9.0)

    def test_builder_is_idempotent(self, warehouse):
        from opendata.pipeline.factor_builder import FactorBuilder

        builder = FactorBuilder(
            warehouse,
            daily_table="test_daily",
            action_table="test_action",
        )
        builder.build()
        builder.build()

        with warehouse.connect() as connection:
            count = connection.execute(
                text("SELECT COUNT(*) FROM `dwd_stock_adjust` WHERE `symbol` = :probe"),
                {"probe": PROBE},
            ).scalar()
        assert count == 2  # upsert, not append


@pytest.mark.e2e
class TestAdjustRest:
    """The REST ``adjust=qfq`` path with real factor rows in place."""

    @pytest.fixture
    def warehouse(self):
        from pathlib import Path

        from opendata.core.config import settings

        engine = create_engine(settings.data_database_url, poolclass=pool.NullPool)
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except Exception as exc:
            pytest.skip(f"warehouse database unreachable: {type(exc).__name__}")
        _upgrade(Path(__file__).resolve().parents[1])
        _ensure_factor_table(engine)
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM `dwd_stock_daily` WHERE `symbol` = :probe"),
                {"probe": PROBE},
            )
            connection.execute(
                text(
                    "INSERT INTO `dwd_stock_daily` "
                    "(`symbol`, `trade_date`, `open`, `high`, `low`, `close`, `volume`, "
                    "`amount`, `source`, `_merged_at`, `_diff_flag`, `_as_of`) VALUES "
                    "(:probe, :d1, 10.0, 10.0, 10.0, 10.0, 100, 1000, 'ths', NOW(), 0, NOW()), "
                    "(:probe, :d2, 12.0, 12.0, 12.0, 12.0, 100, 1200, 'ths', NOW(), 0, NOW())"
                ),
                {"probe": PROBE, "d1": DAY1, "d2": EVENT_DAY},
            )
            connection.execute(
                text(
                    "INSERT INTO `dwd_stock_adjust` "
                    "(`symbol`, `trade_date`, `qfq_factor`, `hfq_factor`, `source`, "
                    "`_merged_at`, `_diff_flag`, `_as_of`) VALUES "
                    "(:probe, :d1, 0.9, 1.0, 'ths', NOW(), 0, NOW()), "
                    "(:probe, :d2, 1.0, :ratio, 'ths', NOW(), 0, NOW())"
                ),
                {"probe": PROBE, "d1": DAY1, "d2": EVENT_DAY, "ratio": 10.0 / 9.0},
            )
        yield engine
        with engine.begin() as connection:
            for table, column in (("dwd_stock_daily", "symbol"), ("dwd_stock_adjust", "symbol")):
                connection.execute(
                    text(f"DELETE FROM `{table}` WHERE `{column}` = :probe"),  # noqa: S608  # literal DDL
                    {"probe": PROBE},
                )
        engine.dispose()

    async def test_adjust_qfq_serves_the_synthesized_series(
        self, warehouse, test_client, test_user_token
    ):
        from opendata.data.providers import register_providers

        register_providers()
        response = await test_client.get(
            "/api/v1/data/equity/stock_daily",
            params={"symbols": PROBE, "start": "2026-09-01", "end": "2026-09-30", "adjust": "qfq"},
            headers={"Authorization": f"Bearer {test_user_token}"},
        )

        assert response.status_code == 200, response.text[:600]
        rows = {row["trade_date"]: row for row in response.json()["data"]["rows"]}
        assert rows[str(DAY1)]["close"] == pytest.approx(9.0)  # 10 x 9/10
        assert rows[str(EVENT_DAY)]["close"] == pytest.approx(12.0)  # anchor day

    async def test_adjust_hfq_serves_the_other_scale(self, warehouse, test_client, test_user_token):
        from opendata.data.providers import register_providers

        register_providers()
        response = await test_client.get(
            "/api/v1/data/equity/stock_daily",
            params={"symbols": PROBE, "start": "2026-09-01", "end": "2026-09-30", "adjust": "hfq"},
            headers={"Authorization": f"Bearer {test_user_token}"},
        )

        assert response.status_code == 200
        rows = {row["trade_date"]: row for row in response.json()["data"]["rows"]}
        assert rows[str(DAY1)]["close"] == pytest.approx(10.0)  # hfq anchor
        assert rows[str(EVENT_DAY)]["close"] == pytest.approx(12.0 * 10.0 / 9.0)


def _upgrade(repo_root) -> None:
    import subprocess
    import sys

    subprocess.run(  # literal argv, no shell
        [sys.executable, "-m", "alembic", "-c", "alembic_data.ini", "upgrade", "head"],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )
