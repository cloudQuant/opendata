"""AC15/D05 - gap continuation, idempotence and recovery on the THS substitute path.

202/AUDIT D05 was: the legacy backfill CLI defaulted to ``skip existing`` and
``ThsHistoryRunner`` decided "already done" from ``MdDataSeries.canonical_id``
alone, so an instrument whose stored window was shorter than the requested one
was dropped from the import list wholesale and a default re-run never closed the
gap.  The accepted case name is
``resume_fills_partial_window_and_remains_idempotent``.

Two layers of the replacement chain are measured here against real databases:

* the durable shard/step checkpoints of
  :func:`scripts.ops.replay_warehouse_pipeline.run_replay` - what a real
  interrupted or partially failed THS window leaves behind, and what the next
  run does with it;
* the ODS -> DWD date window of :func:`opendata.pipeline.dwd_backfill.backfill_dwd`
  with ``source="ths"`` - the only place the replacement path takes an explicit
  *date* range, so head / interior / tail date gaps are seeded and read back
  directly.

Faults are injected below the code under test (a DBAPI ``before_cursor_execute``
guard, and a wrapper around the real ``DwdWriter`` that commits some pages and
then raises), never by a stub that agrees with the expectation by construction.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

import pandas as pd
import pytest
from sqlalchemy import text

from opendata.pipeline.dwd_backfill import DwdBackfillError, backfill_dwd
from tests.ac15_ths_replay_fixture import (
    DWD_TABLE,
    ODS_TABLE,
    build_sandbox,
)
from tests.test_dwd_backfill import _engine_with_tables, _insert, _raw_row

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from pytest import MonkeyPatch

ALL_DAYS = tuple(date(2024, 1, day) for day in (2, 3, 4, 5, 8, 9, 10, 11))
SYMBOLS = ("000001.SZ", "000002.SZ", "600000.SH", "600519.SH", "688981.SH")


def _raw_keys(symbols: Sequence[str], days: Sequence[date]) -> set[str]:
    return {f"{symbol}|{day}" for symbol in symbols for day in days}


def _contract_symbol(raw_symbol: str) -> str:
    return raw_symbol.split(".", maxsplit=1)[0]


def _dwd_frame(days: Sequence[date], *, as_of: date) -> pd.DataFrame:
    """Build one contract-shaped DWD frame for the seeded date band."""
    stamp = datetime(2024, 1, 1)
    return pd.DataFrame(
        [
            {
                "symbol": "000001",
                "trade_date": day,
                "open": 1.0,
                "high": 2.0,
                "low": 0.5,
                "close": 1.5,
                "volume": 100.0,
                "amount": 1000.0,
                "source": "ths",
                "_merged_at": stamp,
                "_diff_flag": 0,
                "_as_of": as_of,
            }
            for day in days
        ]
    )


def _dwd_key_set(engine) -> set[str]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(f"SELECT symbol, trade_date FROM `{DWD_TABLE}`")  # noqa: S608
        ).all()
    return {f"{symbol}|{day}" for symbol, day in rows}


def _duplicate_free(engine, table: str, columns: tuple[str, ...]) -> None:
    """Assert the business key really is unique in one table."""
    projection = ", ".join(f"`{column}`" for column in columns)
    with engine.connect() as connection:
        total = connection.execute(
            text(f"SELECT COUNT(*) FROM `{table}`")  # noqa: S608
        ).scalar_one()
        distinct = connection.execute(
            text(
                f"SELECT COUNT(*) FROM "  # noqa: S608
                f"(SELECT DISTINCT {projection} FROM `{table}`)"
            )
        ).scalar_one()
    assert total == distinct, f"{table} carries duplicate facts ({total} rows, {distinct} keys)"


class _SqliteDwdWriter:
    """The ``BackfillWriter`` seam with a real key-level upsert on SQLite.

    ``opendata.pipeline.dwd_merge.build_upsert_sql`` emits the MySQL
    ``AS new ON DUPLICATE KEY UPDATE`` form, so the production writer cannot run
    against SQLite.  Rows land in the real table through the equivalent
    ``ON CONFLICT`` upsert; nothing about the page, window or checkpoint
    behaviour under test is stubbed.
    """

    def __init__(self, engine) -> None:
        self.engine = engine
        self.calls = 0

    def write(self, frame, *, table: str, key) -> int:
        """Upsert one bounded DWD page and return the submitted row count."""
        self.calls += 1
        if frame.empty:
            return 0
        columns = [str(name) for name in frame.columns]
        sql_columns = ", ".join(f'"{name}"' for name in columns)
        placeholders = ", ".join(f":{name}" for name in columns)
        updates = ", ".join(
            f'"{name}" = excluded."{name}"' for name in columns if name not in set(key)
        )
        statement = text(
            f"INSERT INTO `{table}` ({sql_columns}) VALUES ({placeholders}) "  # noqa: S608
            f"ON CONFLICT ({', '.join(f'`{name}`' for name in key)}) DO UPDATE SET {updates}"
        )
        records = [
            {
                name: (value.to_pydatetime() if isinstance(value, pd.Timestamp) else value)
                for name, value in record.items()
            }
            for record in frame.to_dict("records")
        ]
        with self.engine.begin() as connection:
            connection.execute(statement, records)
        return len(records)


class TestReplayCheckpointContinuation:
    """What a partially landed THS window leaves behind and how a re-run closes it."""

    @pytest.mark.parametrize(("position", "index"), [("first", 0), ("middle", 2), ("tail", 4)])
    def test_gap_at_the_start_middle_and_end_of_the_window_is_closed_by_continuation(
        self, tmp_path: Path, monkeypatch: MonkeyPatch, position: str, index: int
    ) -> None:
        """202/D05: a symbol that already has neighbours must still be completed.

        The legacy runner dropped a whole instrument once a series existed, so a
        short stored window was never widened.  Here one symbol's read is failed
        while the symbols before *and* after it land, which is the same shape,
        and the continuation run has to close exactly that range.
        """
        gap_symbol = SYMBOLS[index]
        others = tuple(symbol for i, symbol in enumerate(SYMBOLS) if i != index)
        sandbox = build_sandbox(tmp_path, monkeypatch, symbols=SYMBOLS, days=ALL_DAYS)
        sandbox.poison_symbol = gap_symbol

        broken = sandbox.run(apply=True, resume=False, shard_size=1)

        assert broken["status"] == "failed"
        landed = sandbox.landed_keys(ODS_TABLE, symbol_column="thscode", date_column="trade_date")
        assert landed == _raw_keys(others, sandbox.days)
        assert not any(key.startswith(f"{gap_symbol}|") for key in landed), position

        sandbox.poison_symbol = None
        resumed = sandbox.run(apply=True, resume=True, shard_size=1)

        assert resumed["status"] == "complete"
        assert resumed["full_coverage_verified"] is True
        # Only the gap is replayed; the already-landed ranges are not re-fetched.
        assert resumed["fetch_symbol_calls"] == 1
        assert resumed["shards_resumed"] == len(others)
        assert sandbox.landed_keys(
            ODS_TABLE, symbol_column="thscode", date_column="trade_date"
        ) == _raw_keys(SYMBOLS, sandbox.days)
        assert sandbox.landed_keys(DWD_TABLE, symbol_column="symbol", date_column="trade_date") == {
            f"{_contract_symbol(symbol)}|{day}" for symbol in SYMBOLS for day in sandbox.days
        }
        _duplicate_free(sandbox.target_engine, ODS_TABLE, ("thscode", "trade_date"))
        _duplicate_free(sandbox.target_engine, DWD_TABLE, ("symbol", "trade_date"))

    def test_resume_after_controlled_interruption_fills_only_the_tail_gap(
        self, tmp_path: Path, monkeypatch: MonkeyPatch
    ) -> None:
        """A run stopped between shards resumes the remainder and never re-fetches the head."""
        sandbox = build_sandbox(tmp_path, monkeypatch, symbols=SYMBOLS, days=ALL_DAYS)
        interrupted = sandbox.run(apply=True, resume=False, shard_size=1, interrupt_after_shards=2)
        assert interrupted["status"] == "interrupted"
        head = SYMBOLS[:2]
        landed = sandbox.landed_keys(ODS_TABLE, symbol_column="thscode", date_column="trade_date")
        assert landed == _raw_keys(head, sandbox.days)

        resumed = sandbox.run(apply=True, resume=True, shard_size=1)
        assert resumed["status"] == "complete"
        assert resumed["full_coverage_verified"] is True
        assert resumed["fetch_symbol_calls"] == len(SYMBOLS) - len(head)
        assert resumed["shards_resumed"] == len(head)
        assert sandbox.landed_keys(
            ODS_TABLE, symbol_column="thscode", date_column="trade_date"
        ) == _raw_keys(SYMBOLS, sandbox.days)
        _duplicate_free(sandbox.target_engine, ODS_TABLE, ("thscode", "trade_date"))

    def test_same_input_rerun_adds_no_duplicate_fact(
        self, tmp_path: Path, monkeypatch: MonkeyPatch
    ) -> None:
        """A completed window replayed with the identical input is a byte-identical no-op."""
        sandbox = build_sandbox(tmp_path, monkeypatch, symbols=SYMBOLS, days=ALL_DAYS)
        sandbox.run(apply=True, resume=False)
        before = sandbox.snapshot()

        again = sandbox.run(apply=True, resume=True)

        assert again["status"] == "complete"
        assert again["fetch_symbol_calls"] == 0
        assert again["rows_written_this_run"] == 0
        after = sandbox.snapshot()
        moved = {
            key: (before[key], after[key]) for key, value in before.items() if after[key] != value
        }
        assert moved == {}, "a rerun with the identical input changed durable state"
        _duplicate_free(sandbox.target_engine, ODS_TABLE, ("thscode", "trade_date"))

    def test_symbol_landed_in_a_failed_shard_is_upserted_not_duplicated(
        self, tmp_path: Path, monkeypatch: MonkeyPatch
    ) -> None:
        """An existing series inside a not-yet-done shard is refreshed in place."""
        sandbox = build_sandbox(tmp_path, monkeypatch, symbols=SYMBOLS, days=ALL_DAYS)
        # Two symbols per shard: the second one fails, so the *first* one is
        # already stored while its shard is still checkpointed as FAILED.
        sandbox.poison_symbol = SYMBOLS[1]
        failed = sandbox.run(apply=True, resume=False, shard_size=2)
        assert failed["status"] == "failed"
        stored = sandbox.landed_keys(ODS_TABLE, symbol_column="thscode", date_column="trade_date")
        assert stored == _raw_keys((SYMBOLS[0], *SYMBOLS[2:]), sandbox.days)
        rows_before = int(sandbox.snapshot()[f"count::target.{ODS_TABLE}"])

        sandbox.poison_symbol = None
        completed = sandbox.run(apply=True, resume=True, shard_size=2)

        assert completed["status"] == "complete"
        assert completed["full_coverage_verified"] is True
        assert sandbox.landed_keys(
            ODS_TABLE, symbol_column="thscode", date_column="trade_date"
        ) == _raw_keys(SYMBOLS, sandbox.days)
        assert int(sandbox.snapshot()[f"count::target.{ODS_TABLE}"]) == rows_before + len(
            sandbox.days
        )
        _duplicate_free(sandbox.target_engine, ODS_TABLE, ("thscode", "trade_date"))
        _duplicate_free(sandbox.target_engine, DWD_TABLE, ("symbol", "trade_date"))

    def test_resume_reports_incomplete_when_a_done_shard_loses_rows(
        self, tmp_path: Path, monkeypatch: MonkeyPatch
    ) -> None:
        """Named limit of the substitute path: series-level completeness is not re-derived.

        This is the residual of 202/D05.  The replacement decides "already done"
        from the durable shard checkpoint, so rows that vanished from a DONE
        shard are not re-fetched by continuation.  It fails closed - the run
        says ``incomplete`` instead of claiming the window - but AC15's "补齐"
        is not satisfied for this state, and no amount of re-running fixes it.
        """
        sandbox = build_sandbox(tmp_path, monkeypatch, symbols=SYMBOLS, days=ALL_DAYS)
        sandbox.run(apply=True, resume=False)
        gone = {f"{SYMBOLS[2]}|{day}" for day in sandbox.days}
        with sandbox.target_engine.begin() as connection:
            for key in sorted(gone):
                symbol, day = key.split("|")
                connection.execute(
                    text(
                        f"DELETE FROM `{ODS_TABLE}` WHERE thscode = :s AND trade_date = :d"  # noqa: S608
                    ),
                    {"s": symbol, "d": day},
                )
            connection.execute(
                text(f"DELETE FROM `{DWD_TABLE}` WHERE symbol = :s"),  # noqa: S608
                {"s": _contract_symbol(SYMBOLS[2])},
            )

        again = sandbox.run(apply=True, resume=True)

        assert again["status"] == "incomplete"
        assert again["full_coverage_verified"] is False
        assert again["fetch_symbol_calls"] == 0
        assert (
            sandbox.landed_keys(ODS_TABLE, symbol_column="thscode", date_column="trade_date") & gone
            == set()
        )

    @pytest.mark.parametrize("gap", ["head", "interior", "tail"])
    def test_dwd_date_window_closes_head_interior_and_tail_gaps(
        self, tmp_path: Path, gap: str
    ) -> None:
        """Date-level gaps for an existing series are closed by the ODS->DWD window.

        ``dwd_backfill`` is the only replacement step that accepts an explicit
        date range, so the 202/D05 wording (a symbol that already exists but
        whose stored window is shorter than the request) is exercised there.
        """
        days = ALL_DAYS
        engine, ods, mapping, _time_column, _time_field = _engine_with_tables(
            "stock_daily", "ths", snapshot_path=tmp_path / "warehouse.sqlite"
        )
        _insert(engine, ods, [_raw_row(mapping, symbol="000001.SZ", day=day) for day in days])
        if gap == "head":
            seeded = days[3:]
        elif gap == "tail":
            seeded = days[:-3]
        else:
            seeded = (days[0], days[2], days[4], days[6])
        missing_before = {day.isoformat() for day in days} - {day.isoformat() for day in seeded}

        _SqliteDwdWriter(engine).write(
            _dwd_frame(seeded, as_of=days[0]), table=DWD_TABLE, key=("symbol", "trade_date")
        )
        assert missing_before
        assert _dwd_key_set(engine) == {f"000001|{day}" for day in seeded}

        stats = backfill_dwd(
            engine,
            domain="stock_daily",
            source="ths",
            start=days[0],
            end=days[-1],
            page_size=3,
            writer=_SqliteDwdWriter(engine),
        )

        assert stats.rows_written == len(days)
        landed_after = _dwd_key_set(engine)
        assert {day.split("|")[1] for day in landed_after} == {day.isoformat() for day in days}
        _duplicate_free(engine, DWD_TABLE, ("symbol", "trade_date"))

        rerun = backfill_dwd(
            engine,
            domain="stock_daily",
            source="ths",
            start=days[0],
            end=days[-1],
            page_size=3,
            writer=_SqliteDwdWriter(engine),
        )
        assert rerun.rows_written == len(days)
        assert _dwd_key_set(engine) == landed_after
        _duplicate_free(engine, DWD_TABLE, ("symbol", "trade_date"))

    def test_dwd_date_window_recovers_after_a_page_committed_then_failed(
        self, tmp_path: Path
    ) -> None:
        """A mid-run failure leaves committed pages that the rerun builds on."""
        days = ALL_DAYS
        engine, ods, mapping, _time_column, _time_field = _engine_with_tables(
            "stock_daily", "ths", snapshot_path=tmp_path / "warehouse.sqlite"
        )
        _insert(engine, ods, [_raw_row(mapping, symbol="000001.SZ", day=day) for day in days])
        real = _SqliteDwdWriter(engine)

        class _FailThirdPage:
            """Commit two real pages, then raise the way a lost connection would."""

            def __init__(self) -> None:
                self.calls = 0

            def write(self, frame, *, table, key):
                self.calls += 1
                if self.calls == 3:
                    raise RuntimeError("injected dwd page failure")
                return real.write(frame, table=table, key=key)

        faulted = _FailThirdPage()
        with pytest.raises(DwdBackfillError):
            backfill_dwd(
                engine,
                domain="stock_daily",
                source="ths",
                start=days[0],
                end=days[-1],
                page_size=3,
                writer=faulted,
            )
        partial = _dwd_key_set(engine)
        assert partial, "the failing run committed nothing before the fault"
        assert len(partial) < len(days)

        recovered = backfill_dwd(
            engine,
            domain="stock_daily",
            source="ths",
            start=days[0],
            end=days[-1],
            page_size=3,
            writer=_SqliteDwdWriter(engine),
        )

        assert recovered.rows_written == len(days)
        assert {day.split("|")[1] for day in _dwd_key_set(engine)} == {
            day.isoformat() for day in days
        }
        assert partial <= _dwd_key_set(engine)
        _duplicate_free(engine, DWD_TABLE, ("symbol", "trade_date"))
