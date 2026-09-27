"""ods writer tests (A4.2).

The ods writer replaces the legacy provider path and fixes its two
defects (design §8.1): tables created without a primary key made
``ON DUPLICATE KEY UPDATE`` a no-op, and a content-hash unique key
turned a corrected price into a new row. The writer upserts on the
**business key**, so re-writing a corrected value updates in place.

Unit tests cover the pure SQL/planning layer plus the ``write``
orchestration against a recording fake - which statements run, in which
order, with which bound rows. The ``e2e`` class owns what only MySQL can
answer: the real upsert semantics, staging/direct equivalence and the
write benchmark.
"""

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine, inspect, pool, text

from opendata.pipeline.ddl import Column, ods_table_ddl
from opendata.pipeline.ods_writer import (
    OdsWriter,
    build_staging_create_sql,
    build_staging_drop_sql,
    build_staging_insert_sql,
    build_staging_upsert_sql,
    build_upsert_sql,
    iter_batches,
    plan_frame,
)

BATCH_ID = "8f14e45f-ceea-467e-b1b3-1d0d5b9f2c11"

#: Columns the scripted fake table reports, in table order.
TABLE_COLUMNS = ["symbol", "trade_date", "close", "_source", "_fetched_at", "_batch_id"]


def _frame(**overrides) -> pd.DataFrame:
    """A two-row source frame shaped like an ods batch."""
    data = {
        "symbol": ["600519", "000001"],
        "trade_date": ["2024-01-02", "2024-01-02"],
        "close": [1688.0, 9.5],
    }
    data.update(overrides)
    return pd.DataFrame(data)


class TestSqlBuilders:
    def test_upsert_uses_business_key_and_alias_form(self):
        sql = build_upsert_sql(
            "ods_x_akshare", ["symbol", "trade_date", "close"], ("symbol", "trade_date")
        )

        assert sql == (
            "INSERT INTO `ods_x_akshare` (`symbol`, `trade_date`, `close`) "
            "VALUES (:symbol, :trade_date, :close) AS new "
            "ON DUPLICATE KEY UPDATE `close` = new.`close`"
        )

    def test_upsert_with_only_key_columns_keeps_a_valid_no_op_update(self):
        sql = build_upsert_sql("ods_x_akshare", ["symbol"], ("symbol",))

        assert sql.endswith("ON DUPLICATE KEY UPDATE `symbol` = new.`symbol`")

    def test_staging_upsert_qualifies_source_columns(self):
        sql = build_staging_upsert_sql(
            "ods_x_akshare",
            "_stg_ods_x_akshare",
            ["symbol", "trade_date", "close"],
            ("symbol", "trade_date"),
        )

        assert sql == (
            "INSERT INTO `ods_x_akshare` (`symbol`, `trade_date`, `close`) "
            "SELECT s.`symbol`, s.`trade_date`, s.`close` FROM `_stg_ods_x_akshare` AS s "
            "ON DUPLICATE KEY UPDATE `close` = s.`close`"
        )

    def test_unsafe_identifier_fails_closed(self):
        with pytest.raises(ValueError):
            build_upsert_sql("ods_x`; DROP TABLE y; --", ["a"], ("a",))


class TestPlanFrame:
    def test_unknown_source_columns_are_ignored_and_reported(self):
        frame = _frame(extra="ignored")

        prepared, plan = plan_frame(
            frame,
            table_columns=["symbol", "trade_date", "close", "_source", "_fetched_at", "_batch_id"],
            key=("symbol", "trade_date"),
            source="akshare",
            batch_id=BATCH_ID,
            fetched_at=datetime(2026, 9, 23, tzinfo=timezone.utc),
        )

        assert plan.ignored == ["extra"]
        assert "extra" not in prepared.columns
        assert list(prepared.columns) == [
            "symbol",
            "trade_date",
            "close",
            "_source",
            "_fetched_at",
            "_batch_id",
        ]

    def test_metadata_columns_are_stamped(self):
        prepared, _ = plan_frame(
            _frame(),
            table_columns=["symbol", "trade_date", "close", "_source", "_fetched_at", "_batch_id"],
            key=("symbol", "trade_date"),
            source="akshare",
            batch_id=BATCH_ID,
            fetched_at=datetime(2026, 9, 23, 12, 30, tzinfo=timezone.utc),
        )

        assert set(prepared["_source"]) == {"akshare"}
        assert set(prepared["_batch_id"]) == {BATCH_ID}
        assert prepared["_fetched_at"].iloc[0] == datetime(2026, 9, 23, 12, 30)

    def test_table_owned_columns_absent_from_the_frame_are_filled_nullable(self):
        prepared, _ = plan_frame(
            _frame(),
            table_columns=[
                "symbol",
                "trade_date",
                "close",
                "turnover",
                "_source",
                "_fetched_at",
                "_batch_id",
            ],
            key=("symbol", "trade_date"),
            source="akshare",
            batch_id=BATCH_ID,
            fetched_at=datetime(2026, 9, 23, tzinfo=timezone.utc),
        )

        assert "turnover" in prepared.columns
        assert prepared["turnover"].isna().all()

    def test_missing_business_key_fails_closed(self):
        with pytest.raises(ValueError, match="business key"):
            plan_frame(
                _frame().drop(columns=["trade_date"]),
                table_columns=["symbol", "close"],
                key=("symbol", "trade_date"),
                source="akshare",
                batch_id=BATCH_ID,
            )

    def test_invalid_batch_id_fails_closed(self):
        with pytest.raises(ValueError, match="batch_id"):
            plan_frame(
                _frame(),
                table_columns=["symbol", "trade_date", "close"],
                key=("symbol", "trade_date"),
                source="akshare",
                batch_id="short",
            )


class TestStatementShapes:
    """The staging SQL the orchestration assembles, verbatim."""

    def test_staging_create_copies_the_structure_and_nothing_else(self):
        assert build_staging_create_sql("_stg_ods_x", "ods_x") == (
            "CREATE TEMPORARY TABLE `_stg_ods_x` AS SELECT * FROM `ods_x` WHERE 1=0"
        )

    def test_staging_insert_is_the_named_placeholder_form(self):
        assert build_staging_insert_sql("_stg_ods_x", ["symbol", "close"]) == (
            "INSERT INTO `_stg_ods_x` (`symbol`, `close`) VALUES (:symbol, :close)"
        )

    def test_staging_drop_is_idempotent(self):
        assert build_staging_drop_sql("_stg_ods_x") == "DROP TEMPORARY TABLE IF EXISTS `_stg_ods_x`"

    def test_an_upsert_without_columns_fails_closed(self):
        with pytest.raises(ValueError, match="without columns"):
            build_upsert_sql("ods_x", [], ("symbol",))

    def test_a_staging_upsert_without_columns_fails_closed(self):
        with pytest.raises(ValueError, match="without columns"):
            build_staging_upsert_sql("ods_x", "_stg_ods_x", [], ("symbol",))


def test_iter_batches_slices_in_order_and_never_exceeds_the_size():
    frame = pd.DataFrame(
        {
            "symbol": [f"{i:06d}" for i in range(5)],
            "trade_date": ["2024-01-02"] * 5,
            "close": [float(i) for i in range(5)],
        }
    )

    chunks = list(iter_batches(frame, 2))

    assert [len(chunk) for chunk in chunks] == [2, 2, 1]
    assert [chunk["symbol"].iloc[0] for chunk in chunks] == ["000000", "000002", "000004"]


def test_iter_batches_rejects_a_non_positive_size():
    with pytest.raises(ValueError, match="must be positive"):
        list(iter_batches(_frame(), 0))


def test_plan_frame_keeps_a_naive_timestamp_as_given():
    """The writer's clock is naive UTC; a naive input must not be shifted."""
    prepared, _ = plan_frame(
        _frame(),
        table_columns=TABLE_COLUMNS,
        key=("symbol", "trade_date"),
        source="akshare",
        batch_id=BATCH_ID,
        fetched_at=datetime(2026, 9, 23, 12, 30),
    )

    assert prepared["_fetched_at"].iloc[0] == datetime(2026, 9, 23, 12, 30)


def test_plan_frame_rejects_a_business_key_the_table_does_not_own():
    with pytest.raises(ValueError, match="not a column of the target table"):
        plan_frame(
            _frame(),
            table_columns=["symbol", "close"],
            key=("symbol", "trade_date"),
            source="akshare",
            batch_id=BATCH_ID,
        )


def test_plan_frame_rejects_a_non_string_batch_id():
    with pytest.raises(ValueError, match="batch_id"):
        plan_frame(
            _frame(),
            table_columns=TABLE_COLUMNS,
            key=("symbol", "trade_date"),
            source="akshare",
            batch_id=None,
        )


class _Result:
    """Write statements need no answer beyond a row count."""

    @property
    def rowcount(self) -> int:
        return 0


class _RecordingConnection:
    def __init__(self, engine: "RecordingEngine", mode: str) -> None:
        self._engine = engine
        self._mode = mode

    def __enter__(self) -> "_RecordingConnection":
        self._engine.opened += 1
        return self

    def __exit__(self, *exc_info: object) -> bool:
        self._engine.closed += 1
        return False

    def execute(self, statement, params=None) -> _Result:
        sql = str(statement)
        self._engine.calls.append((self._mode, sql, params))
        if any(marker in sql for marker in self._engine.reject):
            raise RuntimeError(f"warehouse rejected: {sql[:48]}")
        return _Result()


class RecordingEngine:
    """Warehouse stand-in: one call log, a scripted column list, optional rejects.

    Attributes:
        calls: ``(entry, sql, params)`` per executed statement, in order.
        opened: Connections entered - a staging write must use exactly one.
        reject: Substrings whose statement raises.
    """

    def __init__(self, *, columns: list[str] | None = None, reject: tuple[str, ...] = ()) -> None:
        self.columns = list(TABLE_COLUMNS if columns is None else columns)
        self.reject = reject
        self.calls: list[tuple[str, str, object]] = []
        self.opened = 0
        self.closed = 0

    def begin(self) -> _RecordingConnection:
        return _RecordingConnection(self, "begin")

    def connect(self) -> _RecordingConnection:
        return _RecordingConnection(self, "connect")

    @property
    def statements(self) -> list[str]:
        return [sql for _entry, sql, _params in self.calls]

    @property
    def kinds(self) -> list[str]:
        return [_kind(sql) for sql in self.statements]


def _kind(sql: str) -> str:
    """Name the role of a statement the writer issued."""
    if sql.startswith("CREATE TEMPORARY TABLE"):
        return "create"
    if sql.startswith("DROP TEMPORARY TABLE"):
        return "drop"
    if sql.startswith("TRUNCATE"):
        return "truncate"
    if sql.startswith("INSERT INTO `_stg_"):
        return "fill"
    if sql.startswith("INSERT INTO `ods_"):
        return "staged_upsert" if "SELECT s.`" in sql else "upsert"
    raise AssertionError(f"unexpected statement: {sql!r}")


@pytest.fixture
def scripted(monkeypatch):
    """Recording engine factory, with the table inspector faked too.

    ``OdsWriter.write`` imports ``inspect`` inside the call, so patching
    the module attribute is what lets the orchestration run without a
    database; the columns it reports are the scripted ones.
    """
    box: dict[str, RecordingEngine] = {}

    class _Inspector:
        def get_columns(self, table: str) -> list[dict[str, str]]:
            assert table == "ods_x_akshare"
            return [{"name": name} for name in box["engine"].columns]

    monkeypatch.setattr("sqlalchemy.inspect", lambda target: _Inspector())

    def build(**kwargs: object) -> RecordingEngine:
        engine = RecordingEngine(**kwargs)  # type: ignore[arg-type]
        box["engine"] = engine
        return engine

    return build


def _write(
    engine: RecordingEngine, frame: pd.DataFrame, *, mode: str = "staging", batch_size: int = 2
):
    return OdsWriter(engine, batch_size=batch_size, mode=mode).write(
        frame,
        table="ods_x_akshare",
        key=("symbol", "trade_date"),
        source="akshare",
        batch_id=BATCH_ID,
    )


class TestStagingWrite:
    """The large-volume path: one temp table, one transaction, chunk discipline."""

    def test_an_empty_frame_is_not_a_database_trip(self, scripted):
        engine = scripted()

        result = _write(engine, pd.DataFrame())

        assert (result.rows, result.batches) == (0, 0)
        assert engine.calls == []
        assert (engine.opened, engine.closed) == (0, 0)

    def test_the_statement_sequence_is_create_then_chunk_loop_then_drop(self, scripted):
        engine = scripted()
        frame = _frame(
            symbol=[f"{i:06d}" for i in range(5)],
            trade_date=["2024-01-02"] * 5,
            close=[float(i) for i in range(5)],
        )

        result = _write(engine, frame)

        assert engine.kinds == [
            "create",
            "truncate",
            "fill",
            "staged_upsert",
            "truncate",
            "fill",
            "staged_upsert",
            "truncate",
            "fill",
            "staged_upsert",
            "drop",
        ]
        assert (result.rows, result.batches) == (5, 3)

    def test_the_whole_write_lives_in_one_connection(self, scripted):
        """The staging table is temporary: a second session could not see it."""
        engine = scripted()
        frame = _frame(
            symbol=[f"{i:06d}" for i in range(5)],
            trade_date=["2024-01-02"] * 5,
            close=[float(i) for i in range(5)],
        )

        _write(engine, frame)

        assert engine.opened == 1 == engine.closed
        assert {entry for entry, _sql, _params in engine.calls} == {"begin"}

    def test_each_chunk_fills_the_staging_table_in_isolation(self, scripted):
        engine = scripted()
        frame = _frame(
            symbol=["600519", "000001", "300750"],
            trade_date=["2024-01-02"] * 3,
            close=[1.0, 2.0, 3.0],
        )

        _write(engine, frame)

        fills = [params for _entry, sql, params in engine.calls if _kind(sql) == "fill"]
        assert [len(params) for params in fills] == [2, 1]  # type: ignore[arg-type]
        assert [row["symbol"] for row in fills[0]] == ["600519", "000001"]  # type: ignore[index]
        assert [row["symbol"] for row in fills[1]] == ["300750"]  # type: ignore[index]

    def test_missing_values_reach_the_statement_as_nulls_not_nan(self, scripted):
        engine = scripted()
        frame = _frame(close=[1688.0, np.nan])

        _write(engine, frame)

        fills = [params for _entry, sql, params in engine.calls if _kind(sql) == "fill"]
        assert fills[0][1]["close"] is None  # type: ignore[index]

    def test_the_staging_table_is_dropped_even_when_a_chunk_is_rejected(self, scripted):
        engine = scripted(reject=("INSERT INTO `_stg_",))

        with pytest.raises(RuntimeError, match="warehouse rejected"):
            _write(engine, _frame())

        assert engine.kinds[0] == "create"
        assert engine.kinds[-1] == "drop"
        assert "staged_upsert" not in engine.kinds  # the rejected fill never merged
        assert engine.closed == 1  # the connection was still released

    def test_metadata_columns_are_written_and_the_key_is_never_updated(self, scripted):
        engine = scripted()

        _write(engine, _frame())

        create_sql = next(sql for sql in engine.statements if _kind(sql) == "create")
        staged_upsert = next(sql for sql in engine.statements if _kind(sql) == "staged_upsert")
        assert create_sql == build_staging_create_sql("_stg_ods_x_akshare", "ods_x_akshare")
        assert "s.`_batch_id`" in staged_upsert  # lineage travels with the rows
        assert "`symbol` = s.`symbol`" not in staged_upsert  # the key is not rewritten
        assert "s.`close`" in staged_upsert


class TestDirectWrite:
    """The small-volume path: the alias upsert, no DDL at all."""

    def test_direct_issues_one_upsert_per_chunk_and_no_staging(self, scripted):
        engine = scripted()
        frame = _frame(
            symbol=[f"{i:06d}" for i in range(5)],
            trade_date=["2024-01-02"] * 5,
            close=[float(i) for i in range(5)],
        )

        result = _write(engine, frame, mode="direct")

        assert engine.kinds == ["upsert", "upsert", "upsert"]
        assert (result.rows, result.batches) == (5, 3)

    def test_batch_size_defaults_to_the_design_granularity(self, scripted):
        """默认粒度的行为面：50001 行（预计算行数）走默认配置必须正好 2 个批次。

        只断言 ``DEFAULT_BATCH_SIZE == 50_000`` 是把定义抄一遍（§5.1 的第 2 类空壳），
        而用 ``DEFAULT_BATCH_SIZE + 1`` 去算行数又退回自指——默认值改成 10 万它照绿
        （C44 的反事实正是这么抓出来的）。行数写成字面量后：默认值改大 ⇒ 只剩 1 批，
        改小 ⇒ 批数变多，两种漂移都会红。
        """
        engine = scripted()
        rows = 50_001  # 「刚好超过默认粒度 50_000 一笔」
        frame = pd.DataFrame(
            {
                "symbol": [f"{index:06d}" for index in range(rows)],
                "trade_date": ["2024-01-02"] * rows,
                "close": [1.0] * rows,
            }
        )

        result = OdsWriter(engine, mode="direct").write(
            frame,
            table="ods_x_akshare",
            key=("symbol", "trade_date"),
            source="akshare",
            batch_id=BATCH_ID,
        )

        assert (result.rows, result.batches) == (rows, 2)
        assert engine.kinds == ["upsert", "upsert"]


class TestWriterConfiguration:
    def test_an_unknown_mode_fails_closed_instead_of_writing_nothing(self):
        with pytest.raises(ValueError, match="unknown write mode"):
            OdsWriter(object(), mode="append")  # type: ignore[arg-type]

    def test_a_non_positive_batch_size_fails_closed(self):
        with pytest.raises(ValueError, match="must be positive"):
            OdsWriter(object(), batch_size=0)  # type: ignore[arg-type]


@pytest.mark.e2e
class TestLiveUpsert:
    """Real MySQL semantics: the defects this writer must not repeat."""

    TABLE = "_probe_ods_writer"

    @pytest.fixture
    def warehouse(self):
        from opendata.core.config import settings

        engine = create_engine(settings.data_database_url, poolclass=pool.NullPool)
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except Exception as exc:  # any connection failure means skip
            pytest.skip(f"warehouse database unreachable: {type(exc).__name__}")
        ddl = ods_table_ddl(
            "stock_daily",
            "akshare",
            [
                Column("symbol", "varchar(64)", nullable=False),
                Column("trade_date", "date", nullable=False),
                Column("close", "double", nullable=False),
                Column("ignored_by_writer", "double"),
            ],
            key=("symbol", "trade_date"),
        ).replace("ods_stock_daily_akshare", self.TABLE)
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE IF EXISTS `_probe_ods_writer`"))
            connection.execute(text(ddl))
        yield engine
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE IF EXISTS `_probe_ods_writer`"))
        engine.dispose()

    def _write(self, engine, frame, *, mode="staging"):
        writer = OdsWriter(engine, batch_size=2, mode=mode)
        return writer.write(
            frame,
            table=self.TABLE,
            key=("symbol", "trade_date"),
            source="akshare",
            batch_id=BATCH_ID,
        )

    def test_corrected_value_updates_in_place_without_duplicating(self, warehouse):
        first = _frame()
        # A corrected close for 600519: same business key, new value.
        second = _frame(close=[1700.0, 9.5])

        self._write(warehouse, first)
        self._write(warehouse, second)

        with warehouse.connect() as connection:
            rows = connection.execute(
                text("SELECT symbol, close FROM `_probe_ods_writer` ORDER BY symbol")
            ).all()
        assert rows == [("000001", 9.5), ("600519", 1700.0)]

    def test_staging_and_direct_paths_agree(self, warehouse):
        self._write(warehouse, _frame(), mode="staging")
        with warehouse.connect() as connection:
            staged = connection.execute(
                text("SELECT symbol, close FROM `_probe_ods_writer` ORDER BY symbol")
            ).all()
            connection.execute(text("TRUNCATE TABLE `_probe_ods_writer`"))
        self._write(warehouse, _frame(), mode="direct")
        with warehouse.connect() as connection:
            direct = connection.execute(
                text("SELECT symbol, close FROM `_probe_ods_writer` ORDER BY symbol")
            ).all()

        assert staged == direct

    def test_intra_batch_duplicate_keys_end_with_the_last_value(self, warehouse):
        duplicated = pd.DataFrame(
            {
                "symbol": ["600519", "600519"],
                "trade_date": ["2024-01-02", "2024-01-02"],
                "close": [1.0, 2.0],
            }
        )

        self._write(warehouse, duplicated)

        with warehouse.connect() as connection:
            count = connection.execute(text("SELECT COUNT(*) FROM `_probe_ods_writer`")).scalar()
            close = connection.execute(
                text("SELECT close FROM `_probe_ods_writer` WHERE symbol='600519'")
            ).scalar()
        assert count == 1
        assert close == 2.0

    def test_unknown_columns_never_reach_the_table(self, warehouse):
        result = self._write(warehouse, _frame(extra=[1.0, 2.0]))

        with warehouse.connect() as connection:
            columns = {column["name"] for column in inspect(connection).get_columns(self.TABLE)}
        assert "extra" not in columns
        assert result.batches >= 1

    def test_write_benchmark_records_throughput(self, warehouse):
        rows = 20_000
        frame = pd.DataFrame(
            {
                "symbol": [f"{index % 5000:06d}" for index in range(rows)],
                "trade_date": ["2024-01-02"] * rows,
                "close": [float(index) for index in range(rows)],
            }
        )
        writer = OdsWriter(warehouse, batch_size=10_000)

        started = datetime.now(timezone.utc)
        result = writer.write(
            frame,
            table=self.TABLE,
            key=("symbol", "trade_date"),
            source="akshare",
            batch_id=BATCH_ID,
        )
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()

        assert result.rows == rows
        through_put = rows / max(elapsed, 1e-9)
        print(f"\nods write benchmark: {rows} rows in {elapsed:.2f}s = {through_put:,.0f} rows/s")
        assert through_put > 500  # sanity floor, not a performance gate
