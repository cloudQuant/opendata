"""Notify hook unit tests (AC-11, gate-runnable).

The pure helpers (symbol extraction, the hook factory) are covered first.
The notifier's own warehouse statements are then driven against a
recording fake: what is under test is the *order and shape* of the calls
- which statement runs, in which transaction, with which parameters -
not what MySQL answers for them. That distinction lives in the e2e suite
(AC-13), and this file does not pretend to cover it.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

import pytest

from opendata.pipeline.notify import (
    MAX_FULL_FETCH_ROWS,
    BatchNotifier,
    _symbols_of,
    build_notify_hook,
)
from opendata.pipeline.runner import PipelineContext, Window
from opendata.pipeline.subscription import hub


def _context(keys) -> PipelineContext:
    return PipelineContext(
        domain="stock_daily",
        source="ths",
        window=Window(start=date(2026, 9, 23), end=date(2026, 9, 23)),
        affected_keys=keys,
    )


class TestSymbolsOf:
    def test_symbols_are_deduped_in_first_seen_order(self):
        symbols = _symbols_of(
            _context(
                [
                    ("600519", date(2026, 9, 23)),
                    ("600519", date(2026, 9, 22)),
                    ("000001", date(2026, 9, 23)),
                ]
            )
        )

        assert symbols == ("600519", "000001")

    def test_empty_keys_yield_nothing(self):
        assert _symbols_of(_context([])) == ()

    def test_empty_key_tuples_are_skipped(self):
        assert _symbols_of(_context([(), ("000001", date(2026, 9, 23))])) == ("000001",)


class TestBuildHook:
    def test_returns_a_batch_notifier(self):
        hook = build_notify_hook(object(), batch_id="a" * 36, layer="ods", table="t")

        assert isinstance(hook, BatchNotifier)
        assert callable(hook)

    def test_notifier_defaults_batch_id(self):
        notifier = build_notify_hook(object())

        assert notifier.batch_id is None  # generated per call
        assert notifier.layer == "ods"


class _Result:
    """Stand-in for a SQLAlchemy result: one scalar, or keyed rows."""

    def __init__(
        self,
        *,
        scalar: object = None,
        columns: list[str] | None = None,
        rows: list[tuple] | None = None,
    ):
        self._scalar = scalar
        self._columns = columns or []
        self._rows = rows or []

    def scalar_one(self) -> object:
        return self._scalar

    def keys(self) -> list[str]:
        return self._columns

    def fetchall(self) -> list[tuple]:
        return self._rows


class _Connection:
    """Records which transaction entry point a statement arrived through."""

    def __init__(self, engine: _RecordingEngine, mode: str) -> None:
        self._engine = engine
        self._mode = mode

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def execute(self, statement: Any, params: dict[str, object] | None = None) -> _Result:
        return self._engine.execute(self._mode, str(statement), params or {})


class _RecordingEngine:
    """Warehouse stand-in: answers from a script, keeps an ordered call log.

    Attributes:
        log: ``entry:kind`` per statement, in the order they ran - the
            ordering assertions read this.
        sql: Last statement text seen, per kind.
        params: Last bound parameters, per kind.
    """

    def __init__(
        self,
        *,
        count: int = 7,
        rows: list[dict[str, object]] | None = None,
        fails: tuple[str, ...] = (),
    ):
        self.count = count
        self.rows = rows or []
        self.fails = set(fails)
        self.log: list[str] = []
        self.sql: dict[str, str] = {}
        self.params: dict[str, dict[str, object]] = {}

    def connect(self) -> _Connection:
        return _Connection(self, "connect")

    def begin(self) -> _Connection:
        return _Connection(self, "begin")

    def execute(self, mode: str, sql: str, params: dict[str, object]) -> _Result:
        kind = self.kind_of(sql)
        self.log.append(f"{mode}:{kind}")
        self.sql[kind] = sql
        self.params[kind] = params
        if kind in self.fails:
            raise RuntimeError(f"{kind} read rejected")
        if kind == "count":
            return _Result(scalar=self.count)
        if kind == "read":
            columns = list(self.rows[0]) if self.rows else []
            return _Result(
                columns=columns,
                rows=[tuple(row[column] for column in columns) for row in self.rows],
            )
        return _Result()

    @staticmethod
    def kind_of(sql: str) -> str:
        if sql.startswith("INSERT INTO `batch_watermark`"):
            return "record"
        if sql.startswith("SELECT COUNT(*)"):
            return "count"
        if sql.startswith("SELECT *"):
            return "read"
        raise AssertionError(f"unexpected statement: {sql!r}")


class _Hub:
    """Records what the notifier handed the subscription hub, in call order."""

    def __init__(
        self, *, full: bool = False, error: Exception | None = None, log: list[str] | None = None
    ) -> None:
        self.full = full
        self.error = error
        self.log = log if log is not None else []
        self.published: list[tuple[Any, Any]] = []

    def wants_full(self, event: Any) -> bool:
        return self.full

    async def publish_batch(self, event: Any, *, rows: Any = None) -> int:
        self.log.append("publish")
        if self.error is not None:
            raise self.error
        self.published.append((event, rows))
        return 2


@pytest.fixture
def wired(monkeypatch, request):
    """Notifier + recording engine + stub hub for one scenario."""
    kwargs = getattr(request, "param", {}) or {}
    engine = _RecordingEngine(
        count=kwargs.get("count", 7),
        rows=kwargs.get("rows", []),
        fails=kwargs.get("fails", ()),
    )
    stub = _Hub(full=kwargs.get("full", False), error=kwargs.get("error"), log=engine.log)
    monkeypatch.setattr(hub, "wants_full", stub.wants_full)
    monkeypatch.setattr(hub, "publish_batch", stub.publish_batch)
    notifier = BatchNotifier(engine, batch_id=kwargs.get("batch_id"), table=kwargs.get("table"))
    context = PipelineContext(
        domain="stock_daily",
        source="ths",
        window=Window(start=date(2026, 9, 23), end=date(2026, 9, 24)),
        affected_keys=kwargs.get(
            "keys", [("600519", date(2026, 9, 23)), ("000001", date(2026, 9, 24))]
        ),
    )
    return notifier, engine, stub, context


class TestBatchOrdering:
    """The watermark must be durable before anyone is told about the batch."""

    @pytest.mark.parametrize("wired", [{"full": True}], indirect=True)
    async def test_record_precedes_publish_and_the_read_sits_between(self, wired):
        notifier, engine, _, context = wired

        await notifier(context)

        assert engine.log == ["connect:count", "begin:record", "connect:read", "publish"]

    async def test_recorded_through_a_transaction_not_a_bare_connection(self, wired):
        notifier, engine, _, context = wired

        await notifier(context)

        assert "begin:record" in engine.log  # connect:record would survive no crash
        assert not any(entry == "connect:record" for entry in engine.log)

    async def test_meta_run_never_pays_for_the_batch_read(self, wired):
        notifier, engine, stub, context = wired

        result = await notifier(context)

        assert "read" not in engine.sql
        assert result.rows_read == 0
        assert stub.published[0][1] is None  # rows stay opt-in


class TestWarehouseStatements:
    """What the notifier asks the warehouse, verbatim."""

    @pytest.mark.parametrize(
        "wired",
        [{"batch_id": "b" * 36, "table": "ods_custom_batch", "full": True}],
        indirect=True,
    )
    async def test_count_and_read_are_scoped_to_this_batch_id(self, wired):
        notifier, engine, _, context = wired

        await notifier(context)

        for kind in ("count", "read"):
            assert "`ods_custom_batch`" in engine.sql[kind]
            assert "`_batch_id` = :id" in engine.sql[kind]
            assert engine.params[kind]["id"] == "b" * 36

    @pytest.mark.parametrize("wired", [{"full": True}], indirect=True)
    async def test_full_read_is_capped_at_the_fetch_ceiling(self, wired):
        notifier, engine, _, context = wired

        await notifier(context)

        assert engine.params["read"] == {
            "id": engine.params["record"]["batch_id"],
            "limit": MAX_FULL_FETCH_ROWS,
        }

    @pytest.mark.parametrize(
        "wired",
        [
            {
                "full": True,
                "rows": [
                    {"symbol": "600519", "close": 1.0},
                    {"symbol": "000001", "close": 2.0},
                ],
            }
        ],
        indirect=True,
    )
    async def test_full_run_reports_the_rows_it_actually_read(self, wired):
        notifier, _, stub, context = wired

        result = await notifier(context)

        assert result.rows_read == 2
        assert [row["symbol"] for row in stub.published[0][1]] == ["600519", "000001"]

    async def test_table_derives_from_the_domain_and_source_when_not_overridden(self, wired):
        notifier, engine, _, context = wired

        await notifier(context)

        assert "`ods_stock_daily_ths`" in engine.sql["count"]


class TestFailSoftReads:
    """A warehouse that will not answer must not lose the batch record."""

    @pytest.mark.parametrize("wired", [{"fails": ("count",)}], indirect=True)
    async def test_unreadable_count_still_carries_a_usable_figure(self, wired):
        notifier, engine, stub, context = wired

        result = await notifier(context)

        assert result.watermark.rows == len(context.affected_keys)
        assert "begin:record" in engine.log
        assert stub.published, "the batch was recorded but never announced"

    @pytest.mark.parametrize("wired", [{"full": True, "fails": ("read",)}], indirect=True)
    async def test_unreadable_rows_degrade_the_frame_rather_than_the_event(self, wired):
        notifier, _, stub, context = wired

        result = await notifier(context)

        assert result.rows_read == 0
        assert stub.published[0][1] == []  # empty rows -> hub frames meta, event still sent

    @pytest.mark.parametrize("wired", [{"error": RuntimeError("hub down")}], indirect=True)
    async def test_failed_push_reaches_nobody_but_keeps_the_watermark(self, wired):
        notifier, engine, stub, context = wired

        result = await notifier(context)

        assert (result.delivered, result.rows_read) == (0, 0)
        # The push was still attempted, after the record: a hub that throws
        # must not cost the batch its watermark row, and must not retry.
        assert engine.log == ["connect:count", "begin:record", "publish"]
        assert stub.published == []


class TestBatchId:
    """An omitted batch id is generated once and reused for every statement."""

    async def test_generated_id_reaches_the_watermark_and_both_reads(self, wired):
        notifier, engine, stub, context = wired

        result = await notifier(context)

        batch_id = result.watermark.batch_id
        assert str(uuid.UUID(batch_id)) == batch_id  # a canonical uuid4, the writer's spelling
        assert engine.params["count"]["id"] == batch_id
        assert engine.params["record"]["batch_id"] == batch_id
        assert stub.published[0][0].watermark.batch_id == batch_id

    @pytest.mark.parametrize("wired", [{"batch_id": "a" * 36}], indirect=True)
    async def test_supplied_id_is_not_replaced(self, wired):
        notifier, _, _, context = wired

        result = await notifier(context)

        assert result.watermark.batch_id == "a" * 36
