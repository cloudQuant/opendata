"""fuyao dump 入库测试（A3.3b）。

纯函数层：派生日期（上海语义）、事件幂等键（完全重复塌陷、不同事件不塌陷）、
口径过滤（D10 只留不复权）、列漂移与空结果 fail-closed、源/批次校验。
真机层（``e2e``）：把 dump 导入本地数据仓库，验证键级幂等与行数一致。
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import pandas as pd
import pytest

from opendata.pipeline.dump_import import (
    EVENT_KEY_LENGTH,
    FUYAO_SOURCE,
    DumpImportError,
    event_digest,
    import_adjustment_factor_dump,
    import_daily_k_dump,
    new_batch_id,
    prepare_adjustment_frame,
    prepare_daily_k_frame,
    shanghai_dates,
)

if TYPE_CHECKING:
    from pathlib import Path

try:
    import pyarrow as pa
    import pyarrow.parquet as pq

    PYARROW_AVAILABLE = True
except ImportError:  # pragma: no cover - 取决于环境
    PYARROW_AVAILABLE = False

requires_pyarrow = pytest.mark.skipif(not PYARROW_AVAILABLE, reason="pyarrow is not installed")

DAILY_DUMP = "a_share_daily_k_1d_none_10d"
ADJUSTMENT_DUMP = "a_share_adjustment_factors_event_none_all"

# 2024-01-02 00:00 +08:00
DAY_MS = 1704124800000

_UUID_1 = "11111111-1111-4111-8111-111111111111"
_UUID_2 = "22222222-2222-4222-8222-222222222222"
_UUID_3 = "33333333-3333-4333-8333-333333333333"


def _daily_frame(**overrides: object) -> pd.DataFrame:
    base = {
        "thscode": ["600519.SH", "000001.SZ"],
        "currency": ["CNY", "CNY"],
        "interval": ["1d", "1d"],
        "adjusted": ["none", "none"],
        "date_ms": [DAY_MS, DAY_MS],
        "open_price": [1.0, 2.0],
        "high_price": [1.2, 2.2],
        "low_price": [0.9, 1.9],
        "close_price": [1.1, 2.1],
        "volume": [10.0, 20.0],
        "turnover": [11.0, 42.0],
    }
    base.update(overrides)
    return pd.DataFrame(base)


def _adjustment_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "thscode": ["000812.SZ", "000812.SZ", "000812.SZ"],
            "ticker": ["000812"] * 3,
            "ex_date_ms": [DAY_MS] * 3,
            "dividend_per_share": [0.2, 0.0, 0.2],
            "per_share_bonus": [0.0, 0.1, 0.0],
            "allotment_ratio": [0.0, 0.0, 0.0],
            "allotment_price": [0.0, 0.0, 0.0],
            "currency": ["CNY"] * 3,
        }
    )


class TestDerivations:
    def test_shanghai_dates_map_the_trading_day(self):
        derived = shanghai_dates(pd.Series([DAY_MS]))

        assert derived.iloc[0] == date(2024, 1, 2)

    def test_event_digest_is_stable_and_bounded(self):
        row = _adjustment_frame().iloc[0]

        first = event_digest(row)

        assert first == event_digest(row)
        assert len(first) == EVENT_KEY_LENGTH
        assert all(character in "0123456789abcdef" for character in first)

    def test_event_digest_separates_distinct_events(self):
        frame = _adjustment_frame()
        dividends = event_digest(frame.iloc[0])
        bonuses = event_digest(frame.iloc[1])

        assert dividends != bonuses

    def test_batch_ids_are_canonical_uuids(self):
        import uuid

        batch = new_batch_id()

        assert len(batch) == 36
        assert str(uuid.UUID(batch)) == batch


class TestPrepareDailyK:
    def test_only_unadjusted_rows_survive(self):
        prepared = prepare_daily_k_frame(_daily_frame(adjusted=["none", "forward"]))

        assert len(prepared) == 1
        assert prepared.iloc[0]["thscode"] == "600519.SH"
        assert prepared.iloc[0]["trade_date"] == date(2024, 1, 2)

    def test_derived_date_participates_in_the_frame(self):
        prepared = prepare_daily_k_frame(_daily_frame())

        assert list(prepared.columns) == [
            "thscode",
            "trade_date",
            "date_ms",
            "currency",
            "interval",
            "adjusted",
            "open_price",
            "high_price",
            "low_price",
            "close_price",
            "volume",
            "turnover",
        ]

    def test_schema_drift_fails_closed(self):
        with pytest.raises(DumpImportError, match="missing columns"):
            prepare_daily_k_frame(_daily_frame().drop(columns=["turnover"]))

    def test_dump_without_unadjusted_rows_fails_closed(self):
        with pytest.raises(DumpImportError, match="no 'none' rows"):
            prepare_daily_k_frame(_daily_frame(adjusted=["qfq", "hfq"]))


class TestPrepareAdjustment:
    def test_event_key_collapses_exact_duplicates_only(self):
        prepared = prepare_adjustment_frame(_adjustment_frame())

        assert len(prepared) == 3
        assert prepared["event_key"].nunique() == 2  # 完全重复行塌成同一键
        assert list(prepared["ex_date"].unique()) == [date(2024, 1, 2)]

    def test_schema_drift_fails_closed(self):
        with pytest.raises(DumpImportError, match="missing columns"):
            prepare_adjustment_frame(_adjustment_frame().drop(columns=["allotment_price"]))

    def test_empty_dump_fails_closed(self):
        with pytest.raises(DumpImportError, match="empty"):
            prepare_adjustment_frame(_adjustment_frame().iloc[0:0])


@requires_pyarrow
class TestImportValidation:
    def _write(self, tmp_path: Path, frame: pd.DataFrame, name: str) -> Path:
        path = tmp_path / name
        pq.write_table(pa.Table.from_pandas(frame, preserve_index=False), path)
        return path

    def test_unknown_dump_id_is_refused_before_reading(self, tmp_path: Path):
        path = self._write(tmp_path, _daily_frame(), "daily.parquet")

        with pytest.raises(DumpImportError, match="unregistered dump"):
            import_daily_k_dump(None, path, dump_id="not_a_dump", batch_id=_UUID_1)  # type: ignore[arg-type]

    def test_unknown_source_is_refused(self, tmp_path: Path):
        path = self._write(tmp_path, _daily_frame(), "daily.parquet")

        with pytest.raises(DumpImportError, match="only supports source"):
            import_daily_k_dump(
                None,  # type: ignore[arg-type]
                path,
                dump_id=DAILY_DUMP,
                batch_id=_UUID_1,
                source="akshare",
            )

    def test_non_uuid_batch_id_is_refused(self, tmp_path: Path):
        path = self._write(tmp_path, _daily_frame(), "daily.parquet")

        with pytest.raises(DumpImportError, match="UUID"):
            import_daily_k_dump(None, path, dump_id=DAILY_DUMP, batch_id="b1")  # type: ignore[arg-type]

    def test_blank_batch_id_is_refused(self, tmp_path: Path):
        path = self._write(tmp_path, _daily_frame(), "daily.parquet")

        with pytest.raises(DumpImportError, match="batch_id"):
            import_daily_k_dump(None, path, dump_id=DAILY_DUMP, batch_id="  ")  # type: ignore[arg-type]

    def test_unreadable_file_is_reported(self, tmp_path: Path):
        broken = tmp_path / "broken.parquet"
        broken.write_text("not parquet", encoding="utf-8")

        with pytest.raises(DumpImportError, match="unreadable"):
            import_daily_k_dump(None, broken, dump_id=DAILY_DUMP, batch_id=_UUID_1)  # type: ignore[arg-type]

    def test_source_label_is_ths(self):
        assert FUYAO_SOURCE == "ths"


@pytest.mark.e2e
class TestImportAgainstMysql:
    """真机：dump → ods 入库，键级幂等（重复导入不增行）。"""

    @pytest.fixture
    def warehouse(self):
        from sqlalchemy import create_engine, pool, text

        from opendata.core.config import settings

        engine = create_engine(settings.data_database_url, poolclass=pool.NullPool)
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except Exception as exc:  # any connection failure means skip
            pytest.skip(f"warehouse database unreachable: {type(exc).__name__}")
        import subprocess
        import sys

        subprocess.run(  # literal argv, no shell
            [sys.executable, "-m", "alembic", "-c", "alembic_data.ini", "upgrade", "head"],
            check=True,
            capture_output=True,
        )
        yield engine
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM `ods_stock_daily_ths` WHERE `thscode` LIKE :probe"),
                {"probe": "A33B%"},
            )
            connection.execute(
                text("DELETE FROM `ods_stock_action_ths` WHERE `thscode` LIKE :probe"),
                {"probe": "A33B%"},
            )
        engine.dispose()

    def _parquet(self, tmp_path: Path, frame: pd.DataFrame, name: str) -> Path:
        path = tmp_path / name
        pq.write_table(pa.Table.from_pandas(frame, preserve_index=False), path)
        return path

    def test_daily_k_import_is_key_level_idempotent(self, warehouse, tmp_path: Path):
        from sqlalchemy import text

        frame = _daily_frame(thscode=["A33B1.SH", "A33B2.SZ"])
        path = self._parquet(tmp_path, frame, "daily.parquet")
        try:
            first = import_daily_k_dump(warehouse, path, dump_id=DAILY_DUMP, batch_id=_UUID_1)
            second = import_daily_k_dump(warehouse, path, dump_id=DAILY_DUMP, batch_id=_UUID_2)

            with warehouse.connect() as connection:
                rows = connection.execute(
                    text("SELECT COUNT(*) FROM `ods_stock_daily_ths` WHERE `thscode` LIKE 'A33B%'")
                ).scalar()
                stamped = connection.execute(
                    text(
                        "SELECT `_batch_id` FROM `ods_stock_daily_ths` WHERE `thscode` = 'A33B1.SH'"
                    )
                ).scalar()

            assert first.rows_written == 2
            assert second.rows_written == 2  # 重复导入仍是 upsert，不新增行
            assert rows == 2
            assert stamped == _UUID_2  # 后写批次覆盖留痕
        finally:
            pass

    def test_adjustment_import_keeps_distinct_events(self, warehouse, tmp_path: Path):
        from sqlalchemy import text

        frame = _adjustment_frame()
        frame["thscode"] = ["A33BA.SZ", "A33BA.SZ", "A33BA.SZ"]
        frame["ticker"] = ["A33BA"] * 3
        path = self._parquet(tmp_path, frame, "adjustment.parquet")

        stats = import_adjustment_factor_dump(
            warehouse, path, dump_id=ADJUSTMENT_DUMP, batch_id=_UUID_3
        )

        with warehouse.connect() as connection:
            rows = connection.execute(
                text("SELECT COUNT(*) FROM `ods_stock_action_ths` WHERE `thscode` = 'A33BA.SZ'")
            ).scalar()

        assert stats.rows_selected == 3
        assert rows == 2  # 完全重复行塌陷，另外两条不同事件保留
