"""AC2-18 event-version evidence and factor-hook tests (SQLite only)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Double,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    text,
)
from sqlalchemy.pool import StaticPool

from opendata.pipeline.affected_keys import AffectedKeySpool
from opendata.pipeline.corporate_events import (
    CorporateEventIdentity,
    CorporateEventVersion,
    RawCorporateEventObservation,
)
from opendata.pipeline.factor_builder import FactorBuilder, FactorWriteScope
from opendata.pipeline.factors import FactorEvent, coalesce_same_day_events, cumulate_factors
from opendata.pipeline.notify import build_notify_hook
from opendata.pipeline.subscription import Subscription, hub
from opendata.pipeline.watermark import replay_since
from opendata.services.corporate_events import (
    EVENT_CANDIDATES_TABLE,
    EVENT_CONFLICTS_TABLE,
    EVENT_RECOMPUTE_OUTBOX_TABLE,
    EVENT_VERSIONS_TABLE,
    CorporateEventConflictError,
    CorporateEventIngestService,
    CorporateEventVersionStore,
)

if TYPE_CHECKING:
    import pandas as pd

_EX_DATE = date(2026, 9, 2)
_PRE_EVENT_BASIS = "600519-close-2026-09-01"


def _create_store_tables(engine) -> None:
    """Create the migration-0009 table contract in an isolated SQLite DB."""
    metadata = MetaData()
    Table(
        EVENT_VERSIONS_TABLE,
        metadata,
        Column("source", String(64), primary_key=True),
        Column("event_identity", String(191), primary_key=True),
        Column("revision_order", Integer, primary_key=True),
        Column("content_digest", String(64), primary_key=True),
        Column("identity_basis", String(32), nullable=False),
        Column("economic_event_id", String(191)),
        Column("symbol", String(64), nullable=False),
        Column("ex_date", Date, nullable=False),
        Column("source_revision", String(128), nullable=False),
        Column("pre_event_share_basis", String(191)),
        Column("cash_dividend", Double, nullable=False),
        Column("stock_dividend", Double, nullable=False),
        Column("rights_shares", Double, nullable=False),
        Column("rights_price", Double, nullable=False),
        Column("published_date", Date),
        Column("published_timestamp", DateTime),
        Column("publication_precision", String(16), nullable=False),
        Column("source_timezone", String(64)),
        Column("observed_at", DateTime, nullable=False),
    )
    Table(
        EVENT_CONFLICTS_TABLE,
        metadata,
        Column("source", String(64), primary_key=True),
        Column("event_identity", String(191), primary_key=True),
        Column("revision_order", Integer, primary_key=True),
        Column("digest_a", String(64), primary_key=True),
        Column("digest_b", String(64), primary_key=True),
        Column("detected_at", DateTime, nullable=False),
    )
    Table(
        EVENT_CANDIDATES_TABLE,
        metadata,
        Column("observation_id", String(36), primary_key=True),
        Column("source", String(64), nullable=False),
        Column("content_digest", String(64), nullable=False),
        Column("raw_payload_json", Text, nullable=False),
        Column("symbol", String(64)),
        Column("ex_date", Date),
        Column("observed_at", DateTime, nullable=False),
        Column("identity_status", String(32), nullable=False),
    )
    Table(
        EVENT_RECOMPUTE_OUTBOX_TABLE,
        metadata,
        Column("job_id", String(36), primary_key=True),
        Column("batch_id", String(36), nullable=False, unique=True),
        Column("source", String(64), nullable=False),
        Column("event_identity", String(191), nullable=False),
        Column("revision_order", Integer, nullable=False),
        Column("content_digest", String(64), nullable=False),
        Column("symbol", String(64), nullable=False),
        Column("ex_date", Date, nullable=False),
        Column("status", String(16), nullable=False),
        Column("attempt_count", Integer, nullable=False),
        Column("available_at", DateTime, nullable=False),
        Column("created_at", DateTime, nullable=False),
        Column("lease_token", String(36)),
        Column("lease_until", DateTime),
        Column("last_error", Text),
        Column("completed_at", DateTime),
        Column("factor_row_count", Integer),
        Column("factor_window_start", Date),
        Column("factor_window_end", Date),
    )
    metadata.create_all(engine)


def _event_version(
    *,
    revision_order: int = 1,
    source_revision: str | None = None,
    symbol: str = "600519",
    ex_date: date = _EX_DATE,
    cash: float = 1.0,
    bonus: float = 0.0,
    rights_shares: float = 0.0,
    rights_price: float = 0.0,
    identity: str = "source-action-1",
    economic_event_id: str | None = None,
    share_basis: str | None = _PRE_EVENT_BASIS,
    published_date: date | None = None,
    published_timestamp: datetime | None = None,
    publication_precision: str = "unknown",
    source_timezone: str | None = None,
    observed_at: datetime | None = None,
) -> CorporateEventVersion:
    return CorporateEventVersion(
        identity=CorporateEventIdentity(
            source="fixture",
            event_identity=identity,
            identity_basis="source_event_id",
            symbol=symbol,
            ex_date=ex_date,
            economic_event_id=economic_event_id,
        ),
        source_revision=source_revision or f"revision-{revision_order}",
        revision_order=revision_order,
        observed_at=observed_at or datetime(2026, 10, 1, tzinfo=timezone.utc),
        cash_dividend=cash,
        stock_dividend=bonus,
        rights_shares=rights_shares,
        rights_price=rights_price,
        pre_event_share_basis=share_basis,
        published_date=published_date,
        published_timestamp=published_timestamp,
        publication_precision=publication_precision,
        source_timezone=source_timezone,
    )


def _close_rows() -> list[tuple[str, date, float]]:
    return [
        ("600519", date(2026, 9, 1), 100.0),
        ("600519", _EX_DATE, 101.0),
        ("000001", date(2026, 9, 1), 10.0),
        ("000001", _EX_DATE, 10.2),
    ]


def _create_factor_tables(engine) -> None:
    """Create close and factor tables for a real SQLite factor write."""
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE ods_stock_daily_ths "
                "(thscode TEXT, trade_date DATE, close_price FLOAT, adjusted TEXT)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE dwd_stock_adjust ("
                "symbol TEXT NOT NULL, trade_date DATE NOT NULL, "
                "qfq_factor FLOAT NOT NULL, hfq_factor FLOAT NOT NULL, "
                "qfq_scale FLOAT, qfq_offset FLOAT, hfq_scale FLOAT, hfq_offset FLOAT, "
                "adjustment_version TEXT, legacy_source TEXT, source TEXT, "
                "_merged_at DATETIME, _diff_flag INTEGER, _as_of DATE, "
                "PRIMARY KEY (symbol, trade_date))"
            )
        )
        for symbol, day, close in _close_rows():
            connection.execute(
                text(
                    "INSERT INTO ods_stock_daily_ths "
                    "(thscode, trade_date, close_price, adjusted) "
                    "VALUES (:symbol, :day, :close, 'none')"
                ),
                {"symbol": f"{symbol}.SH", "day": day, "close": close},
            )


def _create_watermark_table(engine) -> None:
    """Create the real watermark schema in SQLite for end-to-end hook tests."""
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE batch_watermark ("
                "seq INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT NOT NULL UNIQUE, "
                "domain TEXT NOT NULL, source TEXT NOT NULL, layer TEXT NOT NULL, "
                "window_start DATE, window_end DATE, rows_written INTEGER NOT NULL, "
                "created_at DATETIME NOT NULL)"
            )
        )


def _install_sqlite_dwd_writer(monkeypatch) -> list[dict[str, object]]:
    """Use a real SQLite upsert transaction in place of MySQL-only SQL text."""
    writes: list[dict[str, object]] = []

    class SQLiteDwdWriter:
        def __init__(self, engine) -> None:
            self.engine = engine

        def write(self, frame, *, table, key):
            assert tuple(key) == ("symbol", "trade_date")
            columns = list(frame.columns)
            records = frame.to_dict(orient="records")
            for record in records:
                for column, value in record.items():
                    if hasattr(value, "to_pydatetime"):
                        record[column] = value.to_pydatetime()
                    elif hasattr(value, "item"):
                        record[column] = value.item()
            quoted = ", ".join(f"`{column}`" for column in columns)
            binds = ", ".join(f":{column}" for column in columns)
            updates = ", ".join(
                f"`{column}` = excluded.`{column}`" for column in columns if column not in key
            )
            statement = text(
                f"INSERT INTO `{table}` ({quoted}) VALUES ({binds}) "  # noqa: S608
                "ON CONFLICT(`symbol`, `trade_date`) DO UPDATE SET "
                f"{updates}"
            )
            with self.engine.begin() as connection:
                connection.execute(statement, records)
            writes.append({"table": table, "rows": len(records), "symbols": tuple(frame["symbol"])})
            return len(records)

    monkeypatch.setattr("opendata.pipeline.factor_builder.DwdWriter", SQLiteDwdWriter)
    return writes


def _factor_service_engine():
    """Create one thread-safe in-memory SQLite warehouse for async service tests."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    _create_store_tables(engine)
    _create_factor_tables(engine)
    _create_watermark_table(engine)
    return engine


def _service(engine, store, *, notify_hook_factory=None):
    """Build the real factor service against the source and DWD fixture tables."""
    builder = FactorBuilder(
        engine,
        daily_table="ods_stock_daily_ths",
        action_table="ods_stock_action_ths",
        event_store=store,
    )
    return CorporateEventIngestService(
        store,
        builder,
        notify_hook_factory=notify_hook_factory,
    )


class TestCorporateEventVersionStore:
    def test_publication_facts_change_content_digest_but_observation_time_does_not(self):
        observed = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
        later_observation = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
        published = _event_version(
            published_date=date(2026, 8, 20),
            publication_precision="date",
            source_timezone="Asia/Shanghai",
            observed_at=observed,
        )
        replay = _event_version(
            published_date=date(2026, 8, 20),
            publication_precision="date",
            source_timezone="Asia/Shanghai",
            observed_at=later_observation,
        )
        changed_announcement = _event_version(
            published_date=date(2026, 8, 21),
            publication_precision="date",
            source_timezone="Asia/Shanghai",
            observed_at=later_observation,
        )
        changed_timezone = _event_version(
            published_date=date(2026, 8, 20),
            publication_precision="date",
            source_timezone="UTC",
            observed_at=later_observation,
        )
        published_at_timestamp = _event_version(
            published_timestamp=datetime(2026, 8, 20, 9, tzinfo=timezone.utc),
            publication_precision="timestamp",
            source_timezone="UTC",
            observed_at=later_observation,
        )

        assert published.content_digest == replay.content_digest
        assert published.content_digest != changed_announcement.content_digest
        assert published.content_digest != changed_timezone.content_digest
        assert published.content_digest != published_at_timestamp.content_digest

    def test_revisions_are_immutable_idempotent_and_latest_revision_wins(self):
        engine = create_engine("sqlite://")
        _create_store_tables(engine)
        store = CorporateEventVersionStore(engine)
        initial = _event_version(revision_order=1, cash=1.0)

        first = store.record_version(initial)
        replay = store.record_version(initial)
        revised = store.record_version(
            _event_version(
                revision_order=2,
                cash=0.5,
                ex_date=date(2026, 9, 3),
            )
        )
        late_old = store.record_version(_event_version(revision_order=0, cash=0.25))

        current = store.load_current_events()
        with engine.connect() as connection:
            persisted = connection.execute(
                text(
                    f"SELECT COUNT(*) FROM `{EVENT_VERSIONS_TABLE}`"  # noqa: S608
                )
            ).scalar_one()

        assert first.status == "inserted"
        assert first.affected_keys == (("600519", _EX_DATE),)
        assert len(first.outbox_job_ids) == 1
        assert replay.status == "idempotent"
        assert replay.affected_keys == ()
        assert replay.outbox_job_ids == ()
        assert revised.status == "inserted"
        assert revised.affected_keys == (("600519", _EX_DATE), ("600519", date(2026, 9, 3)))
        assert late_old.status == "inserted"
        assert late_old.affected_keys == ()
        assert len(current) == 1
        current_by_identity = {event.event_identity: event for event in current}
        assert current_by_identity["source-action-1"].ex_date == date(2026, 9, 3)
        assert current_by_identity["source-action-1"].cash_dividend == 0.5
        assert current_by_identity["source-action-1"].content_digest == revised.content_digest
        assert persisted == 3
        with engine.connect() as connection:
            jobs = connection.execute(
                text(
                    f"SELECT `job_id`, `batch_id`, `created_at` "  # noqa: S608
                    f"FROM `{EVENT_RECOMPUTE_OUTBOX_TABLE}` WHERE `revision_order` = 1"
                )
            ).fetchall()
        assert len(jobs) == 1
        assert jobs[0][0] == first.outbox_job_ids[0]
        engine.dispose()

    def test_same_revision_conflict_persists_both_payloads_and_refuses_current_read(self):
        engine = create_engine("sqlite://")
        _create_store_tables(engine)
        store = CorporateEventVersionStore(engine)

        first = store.record_version(_event_version(cash=1.0))
        conflict = store.record_version(_event_version(cash=2.0))
        replay_conflict_side = store.record_version(_event_version(cash=2.0))
        with engine.connect() as connection:
            versions = connection.execute(
                text(
                    f"SELECT `content_digest`, `cash_dividend` FROM `{EVENT_VERSIONS_TABLE}` "  # noqa: S608
                    "ORDER BY `cash_dividend`"
                )
            ).fetchall()
            conflicts = connection.execute(
                text(
                    f"SELECT `digest_a`, `digest_b` FROM `{EVENT_CONFLICTS_TABLE}`"  # noqa: S608
                )
            ).fetchall()

        assert first.status == "inserted"
        assert conflict.status == "conflict"
        assert conflict.affected_keys == (("600519", _EX_DATE),)
        assert replay_conflict_side.status == "conflict"
        assert replay_conflict_side.affected_keys == ()
        assert [float(row[1]) for row in versions] == [1.0, 2.0]
        assert len(conflicts) == 1
        assert conflicts[0][0] < conflicts[0][1]
        with pytest.raises(CorporateEventConflictError, match="unresolved payload conflict"):
            store.load_current_events()
        engine.dispose()

    def test_same_revision_announcement_metadata_conflict_is_persisted(self):
        engine = create_engine("sqlite://")
        _create_store_tables(engine)
        store = CorporateEventVersionStore(engine)
        first = store.record_version(
            _event_version(
                published_date=date(2026, 8, 20),
                publication_precision="date",
                source_timezone="Asia/Shanghai",
            )
        )
        changed_announcement = store.record_version(
            _event_version(
                published_date=date(2026, 8, 21),
                publication_precision="date",
                source_timezone="Asia/Shanghai",
            )
        )
        with engine.connect() as connection:
            versions = connection.execute(
                text(
                    f"SELECT `published_date`, `content_digest` "  # noqa: S608
                    f"FROM `{EVENT_VERSIONS_TABLE}` ORDER BY `published_date`"
                )
            ).fetchall()
            conflict_count = connection.execute(
                text(f"SELECT COUNT(*) FROM `{EVENT_CONFLICTS_TABLE}`")  # noqa: S608
            ).scalar_one()
            job_count = connection.execute(
                text(f"SELECT COUNT(*) FROM `{EVENT_RECOMPUTE_OUTBOX_TABLE}`")  # noqa: S608
            ).scalar_one()

        assert first.status == "inserted"
        assert changed_announcement.status == "conflict"
        assert first.content_digest != changed_announcement.content_digest
        assert [str(row[0]) for row in versions] == ["2026-08-20", "2026-08-21"]
        assert len({row[1] for row in versions}) == 2
        assert conflict_count == 1
        assert job_count == 2
        with pytest.raises(CorporateEventConflictError, match="unresolved payload conflict"):
            store.load_current_events()
        engine.dispose()

    def test_unresolved_source_identity_preserves_raw_payload_without_event_id(self):
        engine = create_engine("sqlite://")
        _create_store_tables(engine)
        store = CorporateEventVersionStore(engine)
        raw = '{"ticker":"600519.SH","ex_date":"2026-09-02","amount":1.25}'
        observation = RawCorporateEventObservation(
            source="ths",
            raw_payload_json=raw,
            observed_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
            symbol="600519",
            ex_date=_EX_DATE,
        )

        observation_id = store.preserve_unresolved(observation)
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    f"SELECT `observation_id`, `content_digest`, `raw_payload_json`, "  # noqa: S608
                    f"`identity_status` FROM `{EVENT_CANDIDATES_TABLE}`"
                )
            ).one()

        assert row[0] == observation_id
        assert row[0] != observation.content_digest
        assert row[1] == observation.content_digest
        assert row[2] == raw
        assert row[3] == "identity_unresolved"
        assert store.load_current_events() == []
        engine.dispose()


class TestExplicitSameDayFactorAggregation:
    def test_cash_bonus_and_rights_sum_on_one_explicit_share_basis(self):
        events = [
            FactorEvent(
                "600519",
                _EX_DATE,
                cash_dividend=2.0,
                source="official-fixture",
                event_identity="cash-leg",
                pre_event_share_basis=_PRE_EVENT_BASIS,
            ),
            FactorEvent(
                "600519",
                _EX_DATE,
                bonus=0.2,
                source="official-fixture",
                event_identity="bonus-leg",
                pre_event_share_basis=_PRE_EVENT_BASIS,
            ),
            FactorEvent(
                "600519",
                _EX_DATE,
                allotment_ratio=0.1,
                allotment_price=40.0,
                source="official-fixture",
                event_identity="rights-leg-a",
                pre_event_share_basis=_PRE_EVENT_BASIS,
            ),
            FactorEvent(
                "600519",
                _EX_DATE,
                allotment_ratio=0.2,
                allotment_price=30.0,
                source="official-fixture",
                event_identity="rights-leg-b",
                pre_event_share_basis=_PRE_EVENT_BASIS,
            ),
        ]

        combined = coalesce_same_day_events(events)
        factors = cumulate_factors(
            events,
            [("600519", date(2026, 9, 1), 100.0), ("600519", _EX_DATE, 101.0)],
        )

        assert len(combined) == 1
        assert combined[0].cash_dividend == 2.0
        assert combined[0].bonus == pytest.approx(0.2)
        assert combined[0].allotment_ratio == pytest.approx(0.3)
        assert combined[0].allotment_price == pytest.approx(10.0 / 0.3)
        prior = next(factor for factor in factors if factor.trade_date == date(2026, 9, 1))
        assert prior.qfq_scale == pytest.approx(2.0 / 3.0)
        assert prior.qfq_offset == pytest.approx(8.0 / 1.5)
        assert prior.qfq_factor == pytest.approx(1.0 / (150.0 / 108.0))

    def test_unidentified_or_different_basis_same_day_actions_are_refused(self):
        unknown = [
            FactorEvent("600519", _EX_DATE, cash_dividend=1.0, event_key="digest-a"),
            FactorEvent("600519", _EX_DATE, bonus=0.1, event_key="digest-b"),
        ]
        different_basis = [
            FactorEvent(
                "600519",
                _EX_DATE,
                cash_dividend=1.0,
                source="fixture",
                event_identity="cash",
                pre_event_share_basis="basis-a",
            ),
            FactorEvent(
                "600519",
                _EX_DATE,
                bonus=0.1,
                source="fixture",
                event_identity="bonus",
                pre_event_share_basis="basis-b",
            ),
        ]

        with pytest.raises(ValueError, match="ambiguous multiple corporate actions"):
            cumulate_factors(unknown, _close_rows())
        with pytest.raises(ValueError, match="one explicit pre-event share basis"):
            cumulate_factors(different_basis, _close_rows())

    def test_cross_source_mirror_needs_reviewed_identity_and_is_not_double_counted(self):
        mirrors = [
            FactorEvent(
                "600519",
                _EX_DATE,
                cash_dividend=1.0,
                source="source-a",
                event_identity="a-17",
                economic_event_id="reviewed-event-17",
                pre_event_share_basis=_PRE_EVENT_BASIS,
            ),
            FactorEvent(
                "600519",
                _EX_DATE,
                cash_dividend=1.0,
                source="source-b",
                event_identity="b-99",
                economic_event_id="reviewed-event-17",
                pre_event_share_basis=_PRE_EVENT_BASIS,
            ),
        ]
        unreviewed = [
            mirrors[0],
            FactorEvent(
                "600519",
                _EX_DATE,
                cash_dividend=1.0,
                source="source-c",
                event_identity="c-8",
                pre_event_share_basis=_PRE_EVENT_BASIS,
            ),
        ]

        deduped = coalesce_same_day_events(mirrors)
        assert len(deduped) == 1
        assert deduped[0].cash_dividend == 1.0
        with pytest.raises(ValueError, match="cross-source same-day action identity is unresolved"):
            coalesce_same_day_events(unreviewed)


def test_sqlite_reloaded_event_version_and_affected_key_rebuild_existing_factor_hook(
    monkeypatch,
):
    engine = create_engine("sqlite://")
    _create_store_tables(engine)
    store = CorporateEventVersionStore(engine)
    stored = store.record_version(_event_version(cash=1.0, bonus=0.1))
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE ods_stock_daily_ths "
                "(thscode TEXT, trade_date DATE, close_price FLOAT, adjusted TEXT)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE dwd_stock_adjust "
                "(symbol TEXT, trade_date DATE, qfq_factor FLOAT, hfq_factor FLOAT, "
                "source TEXT, legacy_source TEXT)"
            )
        )
        for symbol, day, close in _close_rows():
            connection.execute(
                text(
                    "INSERT INTO ods_stock_daily_ths "
                    "(thscode, trade_date, close_price, adjusted) "
                    "VALUES (:symbol, :day, :close, 'none')"
                ),
                {"symbol": f"{symbol}.SH", "day": day, "close": close},
            )

    written: list[pd.DataFrame] = []

    class WriterSpy:
        def __init__(self, _engine) -> None:
            pass

        def write(self, frame, *, table, key):
            assert table == "dwd_stock_adjust"
            assert key == ("symbol", "trade_date")
            written.append(frame.copy())
            return len(frame)

    monkeypatch.setattr("opendata.pipeline.factor_builder.DwdWriter", WriterSpy)
    builder = FactorBuilder(
        engine,
        daily_table="ods_stock_daily_ths",
        action_table="ods_stock_action_ths",
        event_store=store,
    )
    with AffectedKeySpool() as affected:
        for key in stored.affected_keys:
            affected.append(key, symbol=key[0])
        result = builder.build(affected_keys=affected)

    assert result.rows_written == 2
    assert result.symbols == 1
    assert len(written) == 1
    frame = written[0]
    assert set(frame["symbol"]) == {"600519"}
    before = frame.loc[frame["trade_date"] == date(2026, 9, 1)].iloc[0]
    assert before["qfq_scale"] == pytest.approx(1.0 / 1.1)
    assert before["qfq_offset"] == pytest.approx(-1.0 / 1.1)
    engine.dispose()


def test_factor_builder_rejects_malformed_affected_key_before_database_io():
    class NoIoEngine:
        def connect(self):
            raise AssertionError("affected-key validation must precede database I/O")

    builder = FactorBuilder(
        NoIoEngine(),
        daily_table="ods_stock_daily_ths",
        action_table="ods_stock_action_ths",
    )

    with pytest.raises(ValueError, match="symbol, ex_date"):
        builder.build(affected_keys=[("600519",)])


@pytest.mark.asyncio
async def test_ingest_commits_factors_then_durable_meta_watermark_for_full_subscriber(
    monkeypatch,
):
    engine = _factor_service_engine()
    writes = _install_sqlite_dwd_writer(monkeypatch)
    store = CorporateEventVersionStore(engine)
    service = _service(engine, store)
    messages: list[dict[str, object]] = []

    async def receive(message):
        messages.append(message)

    subscriber = await hub.register(receive)
    subscriber.subscriptions["stock_adjust"] = Subscription(
        domain="stock_adjust",
        layer="dwd",
        payload="full",
        symbols=("600519",),
    )
    try:
        result = await service.ingest(_event_version(cash=1.0))
    finally:
        await hub.unregister(subscriber)

    assert result.version_write.status == "inserted"
    assert result.drain.claimed_jobs == 1
    assert result.drain.completed_jobs == 1
    assert result.drain.failed_jobs == 0
    assert result.drain.rows_written == 2
    assert writes == [{"table": "dwd_stock_adjust", "rows": 2, "symbols": ("600519", "600519")}]
    with engine.connect() as connection:
        factor_rows = connection.execute(
            text(
                "SELECT `symbol`, `trade_date`, `source` FROM `dwd_stock_adjust` "
                "ORDER BY `trade_date`"
            )
        ).fetchall()
        outbox = connection.execute(
            text(
                f"SELECT `status`, `batch_id`, `created_at`, `factor_row_count`, "  # noqa: S608
                "`factor_window_start`, `factor_window_end` "
                f"FROM `{EVENT_RECOMPUTE_OUTBOX_TABLE}`"
            )
        ).one()

    assert [(row[0], str(row[1])) for row in factor_rows] == [
        ("600519", "2026-09-01"),
        ("600519", "2026-09-02"),
    ]
    assert {row[2] for row in factor_rows} == {"corporate-action-affine-v1"}
    assert outbox[0] == "done"
    assert outbox[3:] == (2, "2026-09-01", "2026-09-02")

    replayed = replay_since(engine, domain="stock_adjust", since_batch_id="not-recorded")
    assert len(replayed) == 1
    assert outbox[1] == replayed[0].batch_id
    assert replayed[0].batch_id
    assert replayed[0].domain == "stock_adjust"
    assert replayed[0].source == "corporate-action-affine-v1"
    assert replayed[0].layer == "dwd"
    assert replayed[0].window_start == date(2026, 9, 1)
    assert replayed[0].window_end == date(2026, 9, 2)
    assert replayed[0].rows == 2
    assert replayed[0].to_event()["window"] == {
        "start": "2026-09-01",
        "end": "2026-09-02",
    }
    assert len(messages) == 1
    assert messages[0]["payload"] == "meta"
    assert "data" not in messages[0]
    assert messages[0]["batch_id"] == replayed[0].batch_id
    assert messages[0]["window"] == {"start": "2026-09-01", "end": "2026-09-02"}
    assert messages[0]["rows"] == 2
    engine.dispose()


@pytest.mark.asyncio
async def test_idempotent_ingest_retries_pending_job_with_same_batch_and_creation_time(
    monkeypatch,
):
    engine = _factor_service_engine()
    _install_sqlite_dwd_writer(monkeypatch)
    store = CorporateEventVersionStore(engine)
    calls = 0

    def hook_factory(batch_id, row_count, created_at):
        nonlocal calls
        calls += 1
        if calls == 1:

            async def fail_before_watermark(_context):
                raise RuntimeError("temporary watermark path failure")

            return fail_before_watermark
        return build_notify_hook(
            engine,
            batch_id=batch_id,
            layer="dwd",
            table="dwd_stock_adjust",
            payload_mode="meta",
            row_count=row_count,
            created_at=created_at,
        )

    service = _service(engine, store, notify_hook_factory=hook_factory)
    version = _event_version(cash=1.0)
    first = await service.ingest(version)
    with engine.connect() as connection:
        before_retry = connection.execute(
            text(
                f"SELECT `status`, `batch_id`, `created_at`, `last_error` "  # noqa: S608
                f"FROM `{EVENT_RECOMPUTE_OUTBOX_TABLE}`"
            )
        ).one()
        factor_count = connection.execute(
            text("SELECT COUNT(*) FROM `dwd_stock_adjust`")
        ).scalar_one()
    assert first.drain.failed_jobs == 1
    assert first.drain.completed_jobs == 0
    assert before_retry[0] == "pending"
    assert "temporary watermark path failure" in before_retry[3]
    assert factor_count == 2  # The factor transaction committed before notification.
    assert replay_since(engine, domain="stock_adjust", since_batch_id="not-recorded") == []

    with engine.begin() as connection:
        connection.execute(
            text(
                f"UPDATE `{EVENT_RECOMPUTE_OUTBOX_TABLE}` "  # noqa: S608
                "SET `available_at` = '2000-01-01 00:00:00'"
            )
        )
    replay = await service.ingest(version)
    with engine.connect() as connection:
        after_retry = connection.execute(
            text(
                f"SELECT `status`, `batch_id`, `created_at`, `attempt_count` "  # noqa: S608
                f"FROM `{EVENT_RECOMPUTE_OUTBOX_TABLE}`"
            )
        ).one()
    watermarks = replay_since(engine, domain="stock_adjust", since_batch_id="not-recorded")

    assert replay.version_write.status == "idempotent"
    assert replay.version_write.outbox_job_ids == ()
    assert replay.drain.completed_jobs == 1
    assert replay.drain.failed_jobs == 0
    assert calls == 2
    assert after_retry[0] == "done"
    assert after_retry[1:3] == before_retry[1:3]
    assert after_retry[3] == 2
    assert len(watermarks) == 1
    assert watermarks[0].batch_id == before_retry[1]
    assert watermarks[0].created_at == datetime.fromisoformat(str(before_retry[2]))
    assert watermarks[0].rows == 2
    engine.dispose()


@pytest.mark.asyncio
async def test_same_event_newer_revision_keeps_distinct_outbox_and_watermark_jobs(monkeypatch):
    engine = _factor_service_engine()
    _install_sqlite_dwd_writer(monkeypatch)
    store = CorporateEventVersionStore(engine)
    service = _service(engine, store)

    first = await service.ingest(_event_version(revision_order=1, cash=1.0))
    with engine.connect() as connection:
        first_factor = connection.execute(
            text(
                "SELECT `qfq_factor`, `legacy_source` FROM `dwd_stock_adjust` "
                "WHERE `symbol` = '600519' AND `trade_date` = '2026-09-01'"
            )
        ).one()

    second = await service.ingest(_event_version(revision_order=2, cash=0.5))
    with engine.connect() as connection:
        second_factor = connection.execute(
            text(
                "SELECT `qfq_factor`, `legacy_source` FROM `dwd_stock_adjust` "
                "WHERE `symbol` = '600519' AND `trade_date` = '2026-09-01'"
            )
        ).one()
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                f"SELECT `revision_order`, `status`, `batch_id` "  # noqa: S608
                f"FROM `{EVENT_RECOMPUTE_OUTBOX_TABLE}` ORDER BY `revision_order`"
            )
        ).fetchall()
    watermarks = replay_since(engine, domain="stock_adjust", since_batch_id="not-recorded")

    assert first.version_write.outbox_job_ids != second.version_write.outbox_job_ids
    assert first.drain.completed_jobs == second.drain.completed_jobs == 1
    assert first.drain.failed_jobs == second.drain.failed_jobs == 0
    assert first_factor[0] != second_factor[0]
    assert first_factor[1] == second_factor[1] == "corporate-action-ratio-v1"
    assert [(row[0], row[1]) for row in rows] == [(1, "done"), (2, "done")]
    assert rows[0][2] != rows[1][2]
    assert {item.batch_id for item in watermarks} == {rows[0][2], rows[1][2]}
    assert all(item.rows == 2 for item in watermarks)
    engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("initial_source", "initial_legacy_source", "expected_legacy_source"),
    [
        ("legacy-provider", None, "legacy-provider"),
        ("corporate-action-affine-v1", None, "corporate-action-affine-v1"),
        ("corporate-action-affine-v1", "future-legacy-origin", "future-legacy-origin"),
    ],
    ids=["external-legacy", "null-marker", "unknown-marker"],
)
async def test_unverified_legacy_factor_lineage_survives_two_revisions(
    monkeypatch,
    initial_source,
    initial_legacy_source,
    expected_legacy_source,
):
    engine = _factor_service_engine()
    _install_sqlite_dwd_writer(monkeypatch)
    store = CorporateEventVersionStore(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO `dwd_stock_adjust` "
                "(`symbol`, `trade_date`, `qfq_factor`, `hfq_factor`, `source`, `legacy_source`) "
                "VALUES ('600519', '2026-09-01', 7.0, 8.0, :source, :legacy_source)"
            ),
            {
                "source": initial_source,
                "legacy_source": initial_legacy_source,
            },
        )
    service = _service(engine, store)

    first = await service.ingest(_event_version(revision_order=1, cash=1.0))
    with engine.connect() as connection:
        after_first = connection.execute(
            text(
                "SELECT `qfq_factor`, `hfq_factor`, `source`, `legacy_source` "
                "FROM `dwd_stock_adjust` WHERE `symbol` = '600519' "
                "AND `trade_date` = '2026-09-01'"
            )
        ).one()

    second = await service.ingest(_event_version(revision_order=2, cash=0.5))
    with engine.connect() as connection:
        after_second = connection.execute(
            text(
                "SELECT `qfq_factor`, `hfq_factor`, `source`, `legacy_source` "
                "FROM `dwd_stock_adjust` WHERE `symbol` = '600519' "
                "AND `trade_date` = '2026-09-01'"
            )
        ).one()

    assert first.drain.completed_jobs == second.drain.completed_jobs == 1
    assert first.drain.failed_jobs == second.drain.failed_jobs == 0
    expected = (7.0, 8.0, "corporate-action-affine-v1", expected_legacy_source)
    assert tuple(after_first) == expected
    assert tuple(after_second) == expected
    engine.dispose()


def test_outbox_claim_is_symbol_bounded_lease_cas_and_retry_stable():
    engine = create_engine("sqlite://")
    _create_store_tables(engine)
    store = CorporateEventVersionStore(engine)
    for index in range(51):
        symbol = f"SYM{index:03d}"
        store.record_version(
            _event_version(
                symbol=symbol,
                identity=f"event-{index}",
                share_basis=f"{symbol}-pre-event",
            )
        )

    claimed = store.claim_pending_jobs(
        limit=500,
        max_symbols=50,
        lease_seconds=600,
    )
    assert len(claimed) == 50
    assert len({job.symbol for job in claimed}) == 50
    remaining = store.claim_pending_jobs(limit=500, max_symbols=50, lease_seconds=600)
    assert len(remaining) == 1
    assert remaining[0].symbol not in {job.symbol for job in claimed}
    with engine.begin() as connection:
        connection.execute(
            text(
                f"UPDATE `{EVENT_RECOMPUTE_OUTBOX_TABLE}` "  # noqa: S608
                "SET `lease_until` = '2000-01-01 00:00:00' WHERE `status` = 'processing'"
            )
        )
    reclaimed = store.claim_pending_jobs(
        limit=500,
        max_symbols=50,
        lease_seconds=600,
    )
    old_by_id = {job.job_id: job for job in claimed}
    new_by_id = {job.job_id: job for job in reclaimed}
    assert set(old_by_id) == set(new_by_id)
    for job_id, old_job in old_by_id.items():
        new_job = new_by_id[job_id]
        assert old_job.lease_token != new_job.lease_token
        assert old_job.batch_id == new_job.batch_id
        assert old_job.created_at == new_job.created_at

    with pytest.raises(RuntimeError, match="lease lost"):
        store.complete_jobs(
            claimed,
            FactorWriteScope(
                symbol=claimed[0].symbol,
                row_count=1,
                window_start=date(2026, 9, 1),
                window_end=date(2026, 9, 2),
            ),
        )
    store.retry_jobs((*reclaimed, *remaining), RuntimeError("test retry"))
    with engine.connect() as connection:
        statuses = connection.execute(
            text(
                f"SELECT DISTINCT `status`, `last_error` "  # noqa: S608
                f"FROM `{EVENT_RECOMPUTE_OUTBOX_TABLE}` WHERE `status` != 'pending'"
            )
        ).fetchall()
    assert statuses == []
    engine.dispose()


def test_affected_symbol_rebuild_ignores_other_symbol_close_and_event_conflicts(
    monkeypatch,
):
    engine = create_engine("sqlite://")
    _create_store_tables(engine)
    _create_factor_tables(engine)
    store = CorporateEventVersionStore(engine)
    store.record_version(_event_version(symbol="600519", identity="event-a", cash=1.0))
    store.record_version(_event_version(symbol="000001", identity="event-b", cash=1.0))
    store.record_version(_event_version(symbol="000001", identity="event-b", cash=2.0))
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO ods_stock_daily_ths "
                "(thscode, trade_date, close_price, adjusted) "
                "VALUES ('000001.SZ', '2026-09-01', 10.0, 'none')"
            )
        )
    written: list[tuple[str, ...]] = []

    class WriterSpy:
        def __init__(self, _engine) -> None:
            pass

        def write(self, frame, *, table, key):
            assert table == "dwd_stock_adjust"
            assert key == ("symbol", "trade_date")
            written.append(tuple(sorted(set(frame["symbol"]))))
            return len(frame)

    monkeypatch.setattr("opendata.pipeline.factor_builder.DwdWriter", WriterSpy)
    builder = FactorBuilder(
        engine,
        daily_table="ods_stock_daily_ths",
        action_table="ods_stock_action_ths",
        event_store=store,
    )
    result = builder.build(affected_keys=[("600519", _EX_DATE)])

    assert result.rows_written == 2
    assert result.symbols == 1
    assert written == [("600519",)]
    engine.dispose()
