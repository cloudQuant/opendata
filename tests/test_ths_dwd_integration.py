"""ths ods → dwd 单源直通集成测试（A3+A4，真机 MySQL）。

用探针行走完「ods 源列 → ths 口径映射（含 symbol 去后缀）→ 单源合并直通
→ dwd 键级 upsert → 重算幂等」整条链路；不依赖网络，数据自播种自清理。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from sqlalchemy import Engine


@pytest.mark.e2e
class TestThsSingleSourceDwdPassthrough:
    PROBE = "A3A4PRB"
    DAYS = (date(2026, 1, 5), date(2026, 1, 6))

    @pytest.fixture
    def warehouse(self):
        import subprocess
        import sys

        from sqlalchemy import create_engine, pool, text

        from opendata.core.config import settings

        engine = create_engine(settings.data_database_url, poolclass=pool.NullPool)
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except Exception as exc:  # any connection failure means skip
            pytest.skip(f"warehouse database unreachable: {type(exc).__name__}")
        subprocess.run(  # literal argv, no shell
            [sys.executable, "-m", "alembic", "-c", "alembic_data.ini", "upgrade", "head"],
            check=True,
            capture_output=True,
        )
        yield engine
        with engine.begin() as connection:
            # ods 存源原始列（thscode 带后缀），dwd 存契约列（去后缀）。
            connection.execute(
                text("DELETE FROM `ods_stock_daily_ths` WHERE `thscode` = :probe"),
                {"probe": f"{self.PROBE}.SH"},
            )
            connection.execute(
                text("DELETE FROM `dwd_stock_daily` WHERE `symbol` = :probe"),
                {"probe": self.PROBE},
            )
        engine.dispose()

    def _seed(self, engine: Engine) -> None:
        """Seed two probe rows with the fuyao source-column shape."""
        from sqlalchemy import text

        fetched = datetime.now(timezone.utc).replace(tzinfo=None)
        rows = [
            {
                "thscode": f"{self.PROBE}.SH",
                "trade_date": day.isoformat(),
                "date_ms": int(
                    datetime.combine(
                        day, datetime.min.time(), tzinfo=timezone(timedelta(hours=8))
                    ).timestamp()
                    * 1000
                ),
                "currency": "CNY",
                "interval": "1d",
                "adjusted": "none",
                "open_price": 10.0 + index,
                "high_price": 10.5 + index,
                "low_price": 9.5 + index,
                "close_price": 10.2 + index,
                "volume": 1000.0 + index,
                "turnover": 10200.0 + index,
                "fetched": fetched,
            }
            for index, day in enumerate(self.DAYS)
        ]
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO `ods_stock_daily_ths` "
                    "(`thscode`, `trade_date`, `date_ms`, `currency`, `interval`, `adjusted`, "
                    "`open_price`, `high_price`, `low_price`, `close_price`, `volume`, `turnover`, "
                    "`_source`, `_fetched_at`, `_batch_id`) VALUES ("
                    ":thscode, :trade_date, :date_ms, :currency, :interval, :adjusted, "
                    ":open_price, :high_price, :low_price, :close_price, :volume, :turnover, "
                    "'ths', :fetched, '44444444-4444-4444-8444-444444444444')"
                ),
                rows,
            )

    def test_mapping_merge_and_idempotency(self, warehouse: Engine):
        import asyncio

        from sqlalchemy import text

        from opendata.pipeline.dwd_merge import DwdMergeService, DwdWriter
        from opendata.pipeline.templates import ods_frame_reader

        self._seed(warehouse)

        service = DwdMergeService(
            domain="stock_daily",
            sources=("ths",),
            authority=("ths",),
            readers={"ths": ods_frame_reader(warehouse, "stock_daily", "ths")},
            write_dwd=lambda frame: DwdWriter(warehouse).write(
                frame, table="dwd_stock_daily", key=("symbol", "trade_date")
            ),
            key=("symbol", "trade_date"),
        )

        first = asyncio.run(service.run(*self.DAYS))
        second = asyncio.run(service.run(*self.DAYS))  # 重算幂等（AC-9）

        with warehouse.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT `symbol`, `trade_date`, `close`, `amount`, `source`, `_diff_flag` "
                    "FROM `dwd_stock_daily` WHERE `symbol` = :probe ORDER BY `trade_date`"
                ),
                {"probe": self.PROBE},
            ).all()
            count = connection.execute(
                text("SELECT COUNT(*) FROM `dwd_stock_daily` WHERE `symbol` = :probe"),
                {"probe": self.PROBE},
            ).scalar()

        assert first.passthrough is True and first.rows == 2
        assert second.rows == 2 and second.diff_flagged == 0  # 重算不重复
        assert count == 2
        assert [row[0] for row in rows] == [self.PROBE] * 2  # 后缀已剥离
        assert [row[1] for row in rows] == list(self.DAYS)
        assert rows[0][2] == 10.2 and rows[1][2] == 11.2  # close 经映射
        assert rows[0][3] == 10200.0  # turnover → amount
        assert all(row[4] == "ths" and row[5] == 0 for row in rows)
