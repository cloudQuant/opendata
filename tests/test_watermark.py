"""Batch watermark store tests (AC-11, design §10.2).

The watermark is what makes a reconnect able to replay the batches it
missed, so the properties that matter are ordering (``seq``, never
``created_at``) and idempotency (a retried shard records one event).

The store is MySQL-only (``ON DUPLICATE KEY UPDATE``), so the behaviour
tests live behind the ``e2e`` marker and skip when the warehouse is
unreachable; the pure tests cover the event rendering and the id shape.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import create_engine, pool, text

from opendata.pipeline.watermark import (
    MAX_REPLAY_LIMIT,
    WATERMARK_TABLE,
    BatchWatermark,
    domains_of,
    latest_batches,
    new_batch_id,
    record_batch,
    replay_since,
    utcnow,
)

BASE_TIME = datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)


def _watermark(
    batch_id: str,
    *,
    domain: str = "stock_daily",
    source: str = "ths",
    layer: str = "ods",
    rows: int = 10,
    created_at: datetime | None = None,
) -> BatchWatermark:
    return BatchWatermark(
        batch_id=batch_id,
        domain=domain,
        source=source,
        layer=layer,
        window_start=date(2026, 9, 23),
        window_end=date(2026, 9, 23),
        rows=rows,
        created_at=created_at or BASE_TIME,
    )


class TestPure:
    def test_new_batch_id_is_a_canonical_uuid(self):
        batch_id = new_batch_id()

        assert uuid.UUID(batch_id).version == 4
        assert str(uuid.UUID(batch_id)) == batch_id

    def test_new_batch_ids_do_not_repeat(self):
        assert len({new_batch_id() for _ in range(50)}) == 50

    def test_event_rendering_is_iso_and_complete(self):
        event = _watermark("a" * 36).to_event()

        assert event == {
            "domain": "stock_daily",
            "source": "ths",
            "layer": "ods",
            "batch_id": "a" * 36,
            "window": {"start": "2026-09-23", "end": "2026-09-23"},
            "rows": 10,
            "created_at": BASE_TIME.isoformat(),
        }

    def test_event_rendering_tolerates_a_missing_window(self):
        watermark = BatchWatermark(
            batch_id="b" * 36,
            domain="stock_daily",
            source="ths",
            layer="ods",
            window_start=None,
            window_end=None,
            rows=0,
            created_at=BASE_TIME,
        )

        assert watermark.to_event()["window"] == {"start": None, "end": None}

    def test_utcnow_is_timezone_aware(self):
        assert utcnow().tzinfo is not None

    def test_domains_of_deduplicates_in_order(self):
        batches = [
            _watermark("a" * 36, domain="stock_daily"),
            _watermark("b" * 36, domain="stock_action"),
            _watermark("c" * 36, domain="stock_daily"),
        ]

        assert domains_of(batches) == ["stock_daily", "stock_action"]

    def test_replay_limit_ceiling_is_documented(self):
        assert MAX_REPLAY_LIMIT >= 1_000


class _Result:
    def __init__(self, columns: list[str], rows: list[tuple]) -> None:
        self._columns = columns
        self._rows = rows

    def keys(self) -> list[str]:
        return self._columns

    def fetchall(self) -> list[tuple]:
        return self._rows

    def scalar(self) -> None:
        return None


class _Connection:
    def __init__(self, engine: RecordingWarehouse) -> None:
        self._engine = engine

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def execute(self, statement: Any, params: dict[str, object] | None = None) -> _Result:
        sql = str(statement)
        self._engine.calls.append((self._engine.mode, sql, dict(params or {})))
        return _Result(self._engine.columns, self._engine.rows)


class RecordingWarehouse:
    """Stand-in for the watermark table: records statements, answers with rows.

    Attributes:
        mode: Entry point (``connect`` / ``begin``) of the last statement.
        calls: ``(mode, sql, params)`` per statement, in order.
    """

    def __init__(self, *, rows: list[tuple] | None = None) -> None:
        self.rows = rows or []
        self.calls: list[tuple[str, str, dict[str, object]]] = []
        self.mode = "connect"

    @property
    def columns(self) -> list[str]:
        return [
            "batch_id",
            "domain",
            "source",
            "layer",
            "window_start",
            "window_end",
            "rows_written",
            "created_at",
            "seq",
        ]

    def connect(self) -> _Connection:
        self.mode = "connect"
        return _Connection(self)

    def begin(self) -> _Connection:
        self.mode = "begin"
        return _Connection(self)

    @property
    def sql(self) -> str:
        assert len(self.calls) == 1, f"expected one statement, saw {len(self.calls)}"
        return self.calls[0][1]

    @property
    def params(self) -> dict[str, object]:
        assert len(self.calls) == 1, f"expected one statement, saw {len(self.calls)}"
        return self.calls[0][2]


def _raw_row(seq: int, *, batch_id: str | None = None, domain: str = "stock_daily") -> tuple:
    return (
        batch_id or new_batch_id(),
        domain,
        "ths",
        "ods",
        date(2026, 9, 23),
        date(2026, 9, 24),
        7,
        BASE_TIME,
        seq,
    )


class TestStatementShapes:
    """The replay contract as the store issues it - no MySQL involved.

    What MySQL decides *for* these statements (that ``seq`` really is
    monotonic, that the duplicate key really collapses) is the e2e
    class below; the shape assertions here are the ones that keep
    breaking silently if a query is edited - the ordering column, the
    domain scope, and the bound limit.
    """

    def test_replay_orders_by_seq_and_never_by_a_timestamp(self):
        """created_at collides inside a burst; only seq is a total order."""
        engine = RecordingWarehouse()

        replay_since(engine, domain="stock_daily", since_batch_id="a" * 36)

        assert "ORDER BY `seq` ASC" in engine.sql
        assert "created_at" not in engine.sql.split("ORDER BY")[1]

    def test_replay_is_scoped_to_the_domain_and_starts_after_the_watermark(self):
        engine = RecordingWarehouse()

        replay_since(engine, domain="stock_action", since_batch_id="b" * 36)

        assert engine.params["domain"] == "stock_action"
        assert engine.params["since"] == "b" * 36
        assert "`domain` = :domain" in engine.sql
        # an unknown watermark must not be an error: COALESCE(..., 0) replays all
        assert "COALESCE" in engine.sql

    @pytest.mark.parametrize(
        ("requested", "bounded"),
        [
            (0, 1),
            (-5, 1),
            (50, 50),
            (MAX_REPLAY_LIMIT, MAX_REPLAY_LIMIT),
            (10**9, MAX_REPLAY_LIMIT),
        ],
    )
    def test_the_replay_page_size_is_clamped_into_the_documented_band(self, requested, bounded):
        engine = RecordingWarehouse()

        replay_since(engine, domain="stock_daily", since_batch_id="a" * 36, limit=requested)

        assert engine.params["limit"] == bounded

    def test_replay_rows_come_back_as_watermarks_in_write_order(self):
        engine = RecordingWarehouse(rows=[_raw_row(3), _raw_row(4)])

        batches = replay_since(engine, domain="stock_daily", since_batch_id="a" * 36)

        assert [batch.seq for batch in batches] == [3, 4]
        assert [batch.rows for batch in batches] == [7, 7]
        assert batches[0].created_at == BASE_TIME

    def test_the_tail_query_takes_the_newest_rows_then_restores_write_order(self):
        """A client that says nothing still needs a resync: it gets the last
        ``limit`` batches, oldest first, so it can carry the newest id on."""
        engine = RecordingWarehouse()

        latest_batches(engine, domain="stock_daily", limit=2)

        assert "ORDER BY `seq` DESC LIMIT :limit" in engine.sql
        assert engine.sql.rstrip().endswith("ORDER BY `seq` ASC")
        assert engine.params == {"domain": "stock_daily", "limit": 2}

    @pytest.mark.parametrize("requested", [0, MAX_REPLAY_LIMIT + 1])
    def test_the_tail_page_is_clamped_the_same_way(self, requested):
        engine = RecordingWarehouse()

        latest_batches(engine, domain="stock_daily", limit=requested)

        assert engine.params["limit"] == max(1, min(requested, MAX_REPLAY_LIMIT))

    def test_a_recorded_batch_goes_through_a_transaction_and_carries_its_window(self):
        engine = RecordingWarehouse()

        record_batch(engine, _watermark("c" * 36, rows=12))

        mode, sql, params = engine.calls[0]
        assert mode == "begin"
        assert "ON DUPLICATE KEY UPDATE" in sql  # a retried shard records one event
        assert params["batch_id"] == "c" * 36
        assert params["rows"] == 12
        assert params["window_start"] == date(2026, 9, 23)
        assert params["created_at"] == BASE_TIME

    def test_a_streaming_batch_records_a_null_window(self):
        engine = RecordingWarehouse()

        record_batch(
            engine,
            BatchWatermark(
                batch_id="d" * 36,
                domain="stock_action",
                source="sina",
                layer="dwd",
                window_start=None,
                window_end=None,
                rows=0,
                created_at=BASE_TIME,
            ),
        )

        assert engine.params["window_start"] is None
        assert engine.params["window_end"] is None

    def test_rows_without_a_window_round_trip_as_none(self):
        row = _raw_row(1)
        unwindowed = list(row)
        unwindowed[4] = None
        unwindowed[5] = None
        engine = RecordingWarehouse(rows=[tuple(unwindowed)])

        batch = replay_since(engine, domain="stock_daily", since_batch_id="a" * 36)[0]

        assert (batch.window_start, batch.window_end) == (None, None)
        assert batch.layer == "ods"


@pytest.mark.e2e
class TestAgainstMysql:
    @pytest.fixture
    def warehouse(self):
        from pathlib import Path

        from opendata.core.config import settings

        engine = create_engine(settings.data_database_url, poolclass=pool.NullPool)
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except Exception as exc:  # any connection failure means skip
            pytest.skip(f"warehouse database unreachable: {type(exc).__name__}")
        _upgrade_warehouse(Path(__file__).resolve().parents[1])
        yield engine
        with engine.begin() as connection:
            connection.execute(
                text(
                    f"DELETE FROM `{WATERMARK_TABLE}` "  # noqa: S608  # module constant table
                    "WHERE `domain` LIKE :prefix"
                ),
                {"prefix": "test_%"},
            )
        engine.dispose()

    def test_records_and_replays_only_later_batches(self, warehouse):
        first, second, third = (new_batch_id() for _ in range(3))
        for batch in (first, second, third):
            record_batch(warehouse, _watermark(batch, domain="test_daily"))

        replayed = replay_since(warehouse, domain="test_daily", since_batch_id=first)

        assert [batch.batch_id for batch in replayed] == [second, third]
        assert [batch.seq for batch in replayed] == sorted(batch.seq for batch in replayed)

    def test_recording_the_same_batch_twice_does_not_duplicate(self, warehouse):
        batch = new_batch_id()
        record_batch(warehouse, _watermark(batch, domain="test_daily", rows=10))
        record_batch(warehouse, _watermark(batch, domain="test_daily", rows=12))

        replayed = replay_since(warehouse, domain="test_daily", since_batch_id="0" * 36)

        assert len(replayed) == 1
        assert replayed[0].rows == 12  # the retry's row count wins

    def test_replay_of_an_unknown_watermark_returns_the_tail(self, warehouse):
        for _ in range(3):
            record_batch(warehouse, _watermark(new_batch_id(), domain="test_daily"))

        replayed = replay_since(warehouse, domain="test_daily", since_batch_id="unknown")

        assert len(replayed) == 3  # honest answer to "I lost track"

    def test_latest_batches_are_oldest_first(self, warehouse):
        ids = [new_batch_id() for _ in range(3)]
        for batch in ids:
            record_batch(warehouse, _watermark(batch, domain="test_daily"))

        latest = latest_batches(warehouse, domain="test_daily", limit=2)

        assert [batch.batch_id for batch in latest] == ids[1:]  # the tail, in write order

    def test_replay_is_scoped_to_the_domain(self, warehouse):
        mine, other = new_batch_id(), new_batch_id()
        record_batch(warehouse, _watermark(mine, domain="test_daily"))
        record_batch(warehouse, _watermark(other, domain="test_other"))

        replayed = replay_since(warehouse, domain="test_daily", since_batch_id="0" * 36)

        assert [batch.batch_id for batch in replayed] == [mine]

    def test_window_and_layer_round_trip(self, warehouse):
        batch = new_batch_id()
        record_batch(warehouse, _watermark(batch, domain="test_daily", layer="dwd", rows=7))

        replayed = replay_since(warehouse, domain="test_daily", since_batch_id="0" * 36)

        assert replayed[0].layer == "dwd"
        assert replayed[0].window_start == date(2026, 9, 23)
        assert replayed[0].rows == 7


@pytest.mark.e2e
class TestNotifierAgainstMysql:
    """The step-5 hook records and publishes in one call (design §10.2)."""

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
        _upgrade_warehouse(Path(__file__).resolve().parents[1])
        # Bound the cleanup by sequence, never by domain: the hook records
        # under the real ``stock_daily`` domain, and a domain-wide DELETE
        # would drop the batches other runs had written before this test.
        with engine.connect() as connection:
            start_seq = connection.execute(
                text(f"SELECT COALESCE(MAX(`seq`), 0) FROM `{WATERMARK_TABLE}`")  # noqa: S608
            ).scalar_one()
        yield engine
        with engine.begin() as connection:
            connection.execute(
                text(f"DELETE FROM `{WATERMARK_TABLE}` WHERE `seq` > :from_seq"),  # noqa: S608
                {"from_seq": start_seq},
            )
        engine.dispose()

    async def test_hook_records_the_batch_and_reaches_subscribers(self, warehouse):
        from opendata.pipeline.notify import BatchNotifier
        from opendata.pipeline.runner import PipelineContext, Window
        from opendata.pipeline.subscription import Subscription, hub

        delivered: list[dict] = []

        async def sink(message: dict) -> None:
            delivered.append(message)

        subscriber = await hub.register(sink)
        subscriber.subscriptions["stock_daily"] = Subscription(domain="stock_daily", layer="ods")
        batch = new_batch_id()
        try:
            notifier = BatchNotifier(warehouse, batch_id=batch, layer="ods")
            context = PipelineContext(
                domain="stock_daily",
                source="ths",
                window=Window(start=date(2026, 9, 23), end=date(2026, 9, 23)),
                affected_keys=[("600519", date(2026, 9, 23))],
            )

            result = await notifier(context)
        finally:
            await hub.unregister(subscriber)

        assert result.delivered == 1
        assert result.watermark.batch_id == batch
        assert delivered[0]["type"] == "data.update"
        assert delivered[0]["payload"] == "meta"
        assert delivered[0]["batch_id"] == batch
        assert delivered[0]["domain"] == "stock_daily"
        # the watermark is durable before the push, so a client that got
        # the event can always replay from the id it carries
        replayed = replay_since(warehouse, domain="stock_daily", since_batch_id="0" * 36)
        assert batch in [item.batch_id for item in replayed]

    async def test_full_payload_is_read_back_by_batch_id(self, warehouse):
        from opendata.pipeline.notify import BatchNotifier
        from opendata.pipeline.runner import PipelineContext, Window
        from opendata.pipeline.subscription import Subscription, hub

        batch = new_batch_id()
        with warehouse.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS `test_notify_probe` ("  # literal DDL
                    "`symbol` varchar(16) NOT NULL, `_batch_id` char(36) NOT NULL)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO `test_notify_probe` (`symbol`, `_batch_id`) "
                    "VALUES ('600519', :batch), ('000001', :batch), ('300750', :other)"
                ),
                {"batch": batch, "other": new_batch_id()},
            )
        frames: list[dict] = []

        async def sink(message: dict) -> None:
            frames.append(message)

        subscriber = await hub.register(sink)
        subscriber.subscriptions["stock_daily"] = Subscription(
            domain="stock_daily", layer="ods", payload="full"
        )
        try:
            notifier = BatchNotifier(
                warehouse, batch_id=batch, layer="ods", table="test_notify_probe"
            )
            result = await notifier(
                PipelineContext(
                    domain="stock_daily",
                    source="ths",
                    window=Window(start=date(2026, 9, 23), end=date(2026, 9, 23)),
                    affected_keys=[("600519", date(2026, 9, 23))],
                )
            )
        finally:
            await hub.unregister(subscriber)
            with warehouse.begin() as connection:
                connection.execute(text("DROP TABLE IF EXISTS `test_notify_probe`"))

        assert result.rows_read == 2  # only this batch's rows, not the other batch's
        assert result.watermark.rows == 2
        assert frames[0]["payload"] == "full"
        assert sorted(row["symbol"] for row in frames[0]["data"]) == ["000001", "600519"]

    async def test_meta_subscribers_do_not_pay_for_the_row_read(self, warehouse):
        from opendata.pipeline.notify import BatchNotifier
        from opendata.pipeline.runner import PipelineContext, Window
        from opendata.pipeline.subscription import Subscription, hub

        async def sink(message: dict) -> None:
            pass

        subscriber = await hub.register(sink)
        subscriber.subscriptions["stock_daily"] = Subscription(domain="stock_daily", layer="ods")
        try:
            notifier = BatchNotifier(warehouse, batch_id=new_batch_id(), layer="ods")
            result = await notifier(
                PipelineContext(
                    domain="stock_daily",
                    source="ths",
                    window=Window(start=date(2026, 9, 23), end=date(2026, 9, 23)),
                    affected_keys=[("600519", date(2026, 9, 23))],
                )
            )
        finally:
            await hub.unregister(subscriber)

        assert result.rows_read == 0

    async def test_row_count_falls_back_when_the_table_is_absent(self, warehouse):
        from opendata.pipeline.notify import BatchNotifier
        from opendata.pipeline.runner import PipelineContext, Window

        notifier = BatchNotifier(
            warehouse, batch_id=new_batch_id(), layer="ods", table="no_such_table"
        )
        result = await notifier(
            PipelineContext(
                domain="stock_daily",
                source="ths",
                window=Window(start=date(2026, 9, 23), end=date(2026, 9, 23)),
                affected_keys=[("600519", date(2026, 9, 23)), ("000001", date(2026, 9, 23))],
            )
        )

        assert result.watermark.rows == 2  # the affected-key count stands in

    async def test_hook_does_not_fail_the_pipeline_when_the_push_breaks(self, warehouse):
        from opendata.pipeline.notify import BatchNotifier
        from opendata.pipeline.runner import PipelineContext, Window
        from opendata.pipeline.subscription import Subscription, hub

        async def broken(message: dict) -> None:
            raise RuntimeError("client went away")

        subscriber = await hub.register(broken)
        subscriber.subscriptions["stock_daily"] = Subscription(domain="stock_daily", layer="ods")
        try:
            notifier = BatchNotifier(warehouse, batch_id=new_batch_id(), layer="ods")
            result = await notifier(
                PipelineContext(
                    domain="stock_daily",
                    source="ths",
                    window=Window(start=date(2026, 9, 23), end=date(2026, 9, 23)),
                    affected_keys=[],
                )
            )
        finally:
            await hub.unregister(subscriber)

        assert result.watermark.domain == "stock_daily"  # recorded regardless

    async def test_a_broken_hub_does_not_fail_the_pipeline(self, warehouse):
        from unittest.mock import patch

        from opendata.pipeline.notify import BatchNotifier
        from opendata.pipeline.runner import PipelineContext, Window
        from opendata.pipeline.subscription import hub

        notifier = BatchNotifier(warehouse, batch_id=new_batch_id(), layer="ods")
        with patch.object(hub, "publish_batch", side_effect=RuntimeError("hub exploded")):
            result = await notifier(
                PipelineContext(
                    domain="stock_daily",
                    source="ths",
                    window=Window(start=date(2026, 9, 23), end=date(2026, 9, 23)),
                    affected_keys=[("600519", date(2026, 9, 23))],
                )
            )

        # The data is already durable; a notification problem is not a
        # pipeline failure (design §9.1 step 5 is best-effort).
        assert result.delivered == 0
        assert result.watermark.batch_id  # recorded regardless of the push

    async def test_full_wanted_but_unreadable_table_degrades_silently(self, warehouse):
        from opendata.pipeline.notify import BatchNotifier
        from opendata.pipeline.runner import PipelineContext, Window
        from opendata.pipeline.subscription import Subscription, hub

        frames: list[dict] = []

        async def sink(message: dict) -> None:
            frames.append(message)

        subscriber = await hub.register(sink)
        subscriber.subscriptions["stock_daily"] = Subscription(
            domain="stock_daily", layer="ods", payload="full"
        )
        try:
            notifier = BatchNotifier(
                warehouse, batch_id=new_batch_id(), layer="ods", table="no_such_table"
            )
            result = await notifier(
                PipelineContext(
                    domain="stock_daily",
                    source="ths",
                    window=Window(start=date(2026, 9, 23), end=date(2026, 9, 23)),
                    affected_keys=[("600519", date(2026, 9, 23))],
                )
            )
        finally:
            await hub.unregister(subscriber)

        assert result.rows_read == 0
        assert frames[0]["payload"] == "full"
        assert frames[0]["data"] == []


class TestNotifierHelpers:
    def test_symbols_are_taken_from_the_affected_keys(self):
        from opendata.pipeline.notify import _symbols_of
        from opendata.pipeline.runner import PipelineContext, Window

        context = PipelineContext(
            domain="stock_daily",
            source="ths",
            window=Window(start=date(2026, 9, 23), end=date(2026, 9, 23)),
            affected_keys=[
                ("600519", date(2026, 9, 23)),
                (),
                ("600519", date(2026, 9, 22)),
                ("000001", date(2026, 9, 23)),
            ],
        )

        assert _symbols_of(context) == ("600519", "000001")

    def test_build_notify_hook_returns_a_callable_hook(self):
        from opendata.pipeline.notify import BatchNotifier, build_notify_hook

        hook = build_notify_hook(object(), batch_id="a" * 36, layer="dwd", table="t")

        assert isinstance(hook, BatchNotifier)
        assert callable(hook)


def _upgrade_warehouse(repo_root) -> None:
    """Bring the warehouse schema to head (idempotent)."""
    import subprocess
    import sys

    subprocess.run(  # literal argv, no shell
        [sys.executable, "-m", "alembic", "-c", "alembic_data.ini", "upgrade", "head"],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )


def test_watermark_window_is_optional_for_streaming_domains():
    """A domain without a window still records an event."""
    watermark = BatchWatermark(
        batch_id=new_batch_id(),
        domain="stock_action",
        source="ths",
        layer="dwd",
        window_start=None,
        window_end=None,
        rows=0,
        created_at=BASE_TIME - timedelta(days=1),
    )

    assert watermark.to_event()["rows"] == 0
