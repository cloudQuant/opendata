"""dwd merge tests (A4.6, design §8.3).

The merge layer takes the authority source row by row, falls back to
the next source when the authority has no row for a key (leaving
``source`` as evidence), never lets a non-authority value win a
numeric disagreement - it only raises ``_diff_flag`` - and stamps the
point-in-time columns (``_merged_at``, ``_as_of``). Single-source
domains run in passthrough mode so ``layer=dwd`` stays uniform.

The service's merge unit is the window plus the affected keys, which
is how a corrected old key (adjustment recompute, financial
restatement) propagates into dwd.
"""

from datetime import date, datetime, timezone

import pandas as pd
import pytest

from opendata.pipeline.dwd_merge import (
    DwdMergeService,
    MergeStats,
    merge_source_frames,
)

KEY = ("symbol", "trade_date")
AS_OF = date(2024, 1, 31)
MERGED_AT = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


def _frame(
    rows: list[tuple[str, date, float]], *, source_close_offset: float = 0.0
) -> pd.DataFrame:
    """A contract-shaped stock-daily frame (all dwd value columns)."""
    closes = [row[2] + source_close_offset for row in rows]
    return pd.DataFrame(
        {
            "symbol": [row[0] for row in rows],
            "trade_date": [row[1] for row in rows],
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": [100.0] * len(rows),
            "amount": [1000.0] * len(rows),
        }
    )


AUTHORITY_ROWS = [("600519", date(2024, 1, 2), 1688.0), ("000001", date(2024, 1, 2), 9.5)]


class TestMergeSourceFrames:
    def test_authority_row_wins_and_is_recorded(self):
        merged, stats = merge_source_frames(
            "stock_daily",
            {
                "ths": _frame(AUTHORITY_ROWS),
                "akshare": _frame(AUTHORITY_ROWS, source_close_offset=5.0),
            },
            authority=("ths", "akshare"),
            key=KEY,
            as_of=AS_OF,
            merged_at=MERGED_AT,
        )

        assert stats.rows == 2
        assert stats.degraded_rows == 0
        assert set(merged["source"]) == {"ths"}
        assert merged.loc[merged["symbol"] == "600519", "close"].iloc[0] == 1688.0

    def test_numeric_disagreement_keeps_the_authority_value_and_flags(self):
        merged, stats = merge_source_frames(
            "stock_daily",
            {
                "ths": _frame(AUTHORITY_ROWS),
                "akshare": _frame(AUTHORITY_ROWS, source_close_offset=5.0),
            },
            authority=("ths", "akshare"),
            key=KEY,
            as_of=AS_OF,
            merged_at=MERGED_AT,
        )

        assert stats.diff_flagged == 2
        assert set(merged["_diff_flag"]) == {1}
        # Values stay the authority's, the difference is only marked.
        assert merged.loc[merged["symbol"] == "000001", "close"].iloc[0] == 9.5

    def test_missing_authority_row_degrades_to_the_next_source(self):
        merged, stats = merge_source_frames(
            "stock_daily",
            {
                "ths": _frame(AUTHORITY_ROWS[:1]),
                "akshare": _frame(AUTHORITY_ROWS),
            },
            authority=("ths", "akshare"),
            key=KEY,
            as_of=AS_OF,
            merged_at=MERGED_AT,
        )

        assert stats.rows == 2
        assert stats.degraded_rows == 1
        degraded = merged.loc[merged["symbol"] == "000001"].iloc[0]
        assert degraded["source"] == "akshare"  # 留痕：降级来源可见
        assert degraded["_diff_flag"] == 0  # only one source had the row

    def test_extra_diff_keys_mark_rows_outside_the_disagreement(self):
        merged, stats = merge_source_frames(
            "stock_daily",
            {"ths": _frame(AUTHORITY_ROWS)},
            authority=("ths",),
            key=KEY,
            as_of=AS_OF,
            merged_at=MERGED_AT,
            extra_diff_keys={("000001", date(2024, 1, 2))},
        )

        assert stats.diff_flagged == 1
        assert merged.loc[merged["symbol"] == "000001", "_diff_flag"].iloc[0] == 1

    def test_point_in_time_columns_are_stamped(self):
        merged, _ = merge_source_frames(
            "stock_daily",
            {"ths": _frame(AUTHORITY_ROWS)},
            authority=("ths",),
            key=KEY,
            as_of=AS_OF,
            merged_at=MERGED_AT,
        )

        assert set(merged["_as_of"]) == {AS_OF}
        assert set(merged["_merged_at"]) == {MERGED_AT.replace(tzinfo=None)}

    def test_single_source_domain_is_passthrough(self):
        merged, stats = merge_source_frames(
            "stock_daily",
            {"akshare": _frame(AUTHORITY_ROWS)},
            authority=("akshare",),
            key=KEY,
            as_of=AS_OF,
            merged_at=MERGED_AT,
        )

        assert stats.passthrough is True
        assert stats.diff_flagged == 0
        assert set(merged["source"]) == {"akshare"}

    def test_unknown_authority_source_fails_closed(self):
        with pytest.raises(ValueError, match="authority"):
            merge_source_frames(
                "stock_daily",
                {"akshare": _frame(AUTHORITY_ROWS)},
                authority=("ths",),
                key=KEY,
                as_of=AS_OF,
                merged_at=MERGED_AT,
            )


def _reader_for(frames: dict[str, pd.DataFrame], name: str):
    """A reader honouring the contract: window rows plus affected keys."""

    def read(start: date, end: date, affected_keys: set[tuple]) -> pd.DataFrame:
        frame = frames[name].copy()
        for symbol, trade_date in affected_keys:
            present = ((frame["symbol"] == symbol) & (frame["trade_date"] == trade_date)).any()
            if not present:
                frame = pd.concat(
                    [frame, _frame([(symbol, trade_date, 1.0)])],
                    ignore_index=True,
                )
        return frame

    return read


class TestDwdMergeService:
    def _service(self, frames, **overrides):
        written: list[pd.DataFrame] = []
        service = DwdMergeService(
            "stock_daily",
            sources=("ths", "akshare"),
            authority=("ths", "akshare"),
            readers={name: _reader_for(frames, name) for name in ("ths", "akshare")},
            write_dwd=lambda frame: written.append(frame) or len(frame),
            key=KEY,
            merged_at=MERGED_AT,
            **overrides,
        )
        return service, written

    async def test_window_merge_writes_the_merged_frame(self):
        service, written = self._service(
            {
                "ths": _frame(AUTHORITY_ROWS),
                "akshare": _frame(AUTHORITY_ROWS, source_close_offset=1.0),
            }
        )

        stats = await service.run(date(2024, 1, 1), date(2024, 1, 31))

        assert isinstance(stats, MergeStats)
        assert stats.rows == 2
        assert written and len(written[0]) == 2

    async def test_affected_keys_extend_the_merge_unit(self):
        """修订传播: a corrected older key is recomputed with the window."""
        service, written = self._service(
            {"ths": _frame(AUTHORITY_ROWS), "akshare": _frame(AUTHORITY_ROWS)}
        )

        stats = await service.run(
            date(2024, 1, 1),
            date(2024, 1, 31),
            affected_keys={("600519", date(2023, 12, 29))},
        )

        assert stats.rows == 3  # window rows plus the affected key
        assert ("600519", date(2023, 12, 29)) in set(
            zip(written[0]["symbol"], written[0]["trade_date"], strict=True)
        )

    async def test_key_can_come_from_the_source_mapping(self):
        from opendata.data.mapping import DomainMapping, FieldMapping

        frames = {"ths": _frame(AUTHORITY_ROWS), "akshare": _frame(AUTHORITY_ROWS)}
        written: list[pd.DataFrame] = []
        service = DwdMergeService(
            "stock_daily",
            sources=("ths", "akshare"),
            authority=("ths",),
            readers={name: _reader_for(frames, name) for name in ("ths", "akshare")},
            write_dwd=lambda frame: written.append(frame) or len(frame),
            mappings={
                "ths": DomainMapping(
                    domain="stock_daily",
                    key=KEY,
                    fields={
                        "symbol": FieldMapping("symbol"),
                        "trade_date": FieldMapping("trade_date"),
                        "close": FieldMapping("close"),
                    },
                )
            },
            merged_at=MERGED_AT,
        )

        stats = await service.run(date(2024, 1, 1), date(2024, 1, 31))

        assert stats.rows == 2

    async def test_missing_key_definition_fails_closed(self):
        frames = {"ths": _frame(AUTHORITY_ROWS)}
        service = DwdMergeService(
            "stock_daily",
            sources=("ths",),
            authority=("ths",),
            readers={"ths": _reader_for(frames, "ths")},
            write_dwd=lambda frame: len(frame),
            merged_at=MERGED_AT,
        )

        with pytest.raises(ValueError, match="business key"):
            await service.run(date(2024, 1, 1), date(2024, 1, 31))

    async def test_hook_consumes_the_pipeline_context(self):
        from opendata.pipeline.runner import PipelineContext, Window

        service, written = self._service(
            {"ths": _frame(AUTHORITY_ROWS), "akshare": _frame(AUTHORITY_ROWS)}
        )
        context = PipelineContext(
            domain="stock_daily",
            source="akshare",
            window=Window(start=date(2024, 1, 1), end=date(2024, 1, 31)),
            affected_keys=[("600519", date(2024, 1, 2))],
        )

        stats = await service.run_hook(context)

        assert stats.rows == 2
        assert written


@pytest.mark.e2e
class TestDwdWriteAgainstMysql:
    TABLE = "dwd_stock_daily"

    def test_merge_writes_rows_with_trace_columns_and_is_idempotent(self):
        from sqlalchemy import create_engine, pool, text

        from opendata.core.config import settings
        from opendata.pipeline.dwd_merge import DwdWriter

        engine = create_engine(settings.data_database_url, poolclass=pool.NullPool)
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except Exception as exc:  # any connection failure means skip
            pytest.skip(f"warehouse database unreachable: {type(exc).__name__}")
        try:
            merged, _ = merge_source_frames(
                "stock_daily",
                {"ths": _frame(AUTHORITY_ROWS)},
                authority=("ths",),
                key=KEY,
                as_of=AS_OF,
                merged_at=MERGED_AT,
            )
            writer = DwdWriter(engine)
            writer.write(merged, table=self.TABLE, key=KEY)
            # Re-running the same merge must update in place.
            merged.loc[:, "close"] = merged["close"] + 1.0
            writer.write(merged, table=self.TABLE, key=KEY)

            with engine.connect() as connection:
                rows = connection.execute(
                    text(
                        "SELECT symbol, close, source, _diff_flag, _as_of "
                        "FROM `dwd_stock_daily` WHERE symbol = '600519'"
                    )
                ).all()
            assert len(rows) == 1
            assert rows[0][0] == "600519"
            assert rows[0][1] == pytest.approx(1689.0)
            assert rows[0][2] == "ths"
            assert rows[0][3] == 0
            assert rows[0][4] == AS_OF
        finally:
            with engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM `dwd_stock_daily` WHERE symbol IN ('600519', '000001')")
                )
            engine.dispose()
