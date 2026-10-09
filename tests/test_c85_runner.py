"""Runner orchestration failure legs, driven entirely by injected fakes.

The six-step engine's only durable seam is ``session_maker``; every test
here runs the real ``_run_locked_impl`` on a subclass that records the
bookkeeping calls instead of issuing them, so the failure paths can be
observed without a main database, a warehouse or a lock collision.
"""

import pathlib
import sys
import types
import typing
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import date
from typing import Any

import pandas as pd
import pytest

from opendata.models.pipeline import ShardStatus
from opendata.pipeline import runner
from opendata.pipeline.affected_keys import SymbolPartitioning
from opendata.pipeline.runner import (
    DataPipeline,
    PipelineContext,
    PipelineSpec,
    ShardFailure,
    Window,
    release_pipeline_lock,
)

WINDOW = Window(start=date(2026, 9, 22), end=date(2026, 9, 24))
EARLIER = Window(start=date(2026, 9, 18), end=date(2026, 9, 24))
#: The pipeline's session factory is never touched by the recorded seams.
NO_SESSION = object()


def _frame(symbol: str, day: date = date(2026, 9, 22)) -> pd.DataFrame:
    """One landed row in the ods spelling the key extractor reads."""
    return pd.DataFrame({"symbol": [symbol], "trade_date": [day], "close": [1.0]})


def _plain(symbol: object) -> str:
    """Normalize ``600519.SH`` to the contract symbol ``600519``."""
    return str(symbol).split(".", 1)[0]


class RecordingPipeline(DataPipeline):
    """The real orchestration with the durable bookkeeping recorded."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Start every ledger empty."""
        super().__init__(*args, **kwargs)
        self.shard_records: list[tuple[int, ShardStatus, int, str | None]] = []
        self.step_records: list[tuple[str, str]] = []
        self.invalidated: list[str] = []
        self.cleared: list[str] = []
        self.saved_plans: list[dict[str, dict[str, Window | None]]] = []

    async def _completed_shards(self, pipeline_id: str) -> set[int]:
        """Answer from the injected resume set (no main-database read)."""
        return set()

    async def _clear_step_state(self, pipeline_id: str) -> None:
        """Record the reset instead of deleting checkpoint rows."""
        self.cleared.append(pipeline_id)

    async def _record(
        self,
        pipeline_id: str,
        shard: int,
        window: Window,
        status: ShardStatus,
        *,
        rows: int = 0,
        error: str | None = None,
    ) -> None:
        """Append one shard progress write."""
        self.shard_records.append((shard, status, rows, error))

    async def _invalidate_hooks(self, pipeline_id: str) -> None:
        """Record the pre-commit invalidation."""
        self.invalidated.append(pipeline_id)

    async def _step_done(self, pipeline_id: str, step: str) -> bool:
        """Nothing is checkpointed yet, so every step has to run."""
        return False

    async def _record_step(self, pipeline_id: str, step: str, status: str, *, error=None) -> None:
        """Append one step-checkpoint write."""
        self.step_records.append((step, status))

    async def _load_source_windows(self, pipeline_id: str) -> dict:
        """First run of this plan: no persisted windows."""
        return {}

    async def _save_source_windows(self, pipeline_id: str, windows: Mapping) -> None:
        """Append the persisted per-source plan."""
        self.saved_plans.append({source: dict(plan) for source, plan in windows.items()})


def _spec(symbols: Sequence[str], *, source: str, shard_size: int = 2) -> PipelineSpec:
    """A stock-daily spec; each test uses its own source so locks never collide."""
    return PipelineSpec(
        domain="stock_daily",
        source=source,
        key=("symbol", "trade_date"),
        symbols=list(symbols),
        shard_size=shard_size,
    )


@pytest.fixture(autouse=True)
def _release_probe_locks() -> Any:
    """Drop the process locks the probe sources take."""
    yield
    for source in ("ods-failure", "bounded-keys", "rebuilt-keys", "fetch-shard"):
        release_pipeline_lock(f"stock_daily:{source}")


async def test_ods_write_failure_fails_only_that_shard_and_the_run_continues() -> None:
    calls: list[pd.DataFrame] = []
    contexts: list[PipelineContext] = []

    def write_ods(frame: pd.DataFrame) -> int:
        calls.append(frame)
        if len(calls) == 1:
            raise RuntimeError("ods volume is full")
        return len(frame)

    def fetch(symbol: str, window: Window) -> pd.DataFrame:
        assert window == WINDOW
        return _frame(symbol)

    async def cross_check(context: PipelineContext) -> str:
        contexts.append(context)
        return "compared"

    pipeline = RecordingPipeline(
        _spec(["000000", "000001", "000002", "000003"], source="ods-failure"),
        session_maker=NO_SESSION,
        write_ods=write_ods,
        fetch_symbol=fetch,
        cross_check=cross_check,
    )
    outcome = await pipeline.run(WINDOW, resume=False)

    assert outcome.shards_total == 2
    assert outcome.shards_failed == 1
    assert outcome.shards_done == 1
    assert outcome.rows_written == 2
    assert outcome.failures == [ShardFailure(0, None, "ods volume is full")]
    # Running -> failed for the broken shard, running -> done for the rest.
    assert pipeline.shard_records == [
        (0, ShardStatus.RUNNING, 0, None),
        (0, ShardStatus.FAILED, 0, "ods volume is full"),
        (1, ShardStatus.RUNNING, 0, None),
        (1, ShardStatus.DONE, 2, None),
    ]
    assert pipeline.invalidated == [pipeline._pipeline_id(WINDOW)] * 2
    assert pipeline.cleared == [pipeline._pipeline_id(WINDOW)]
    assert pipeline.step_records == [("cross_check", "running"), ("cross_check", "done")]
    # The hook only ever sees the shard that actually landed.
    assert contexts[0].affected_keys == [
        ("000002", date(2026, 9, 22)),
        ("000003", date(2026, 9, 22)),
    ]
    assert [sorted(frame["symbol"]) for frame in calls] == [
        ["000000", "000001"],
        ["000002", "000003"],
    ]


async def test_affected_key_outside_the_request_universe_fails_closed() -> None:
    written: list[pd.DataFrame] = []

    def fetch(symbol: str, window: Window) -> pd.DataFrame:
        # The source answers for a different symbol than the one requested.
        return _frame("000001.SZ")

    pipeline = RecordingPipeline(
        _spec(["600519.SH"], source="bounded-keys", shard_size=5),
        session_maker=NO_SESSION,
        write_ods=lambda frame: written.append(frame) or len(frame),
        fetch_symbol=fetch,
        bounded_partitions=SymbolPartitioning(
            symbol_key_index=0, normalize_symbol=_plain, batch_size=2
        ),
    )

    with pytest.raises(
        ValueError, match="affected key symbol is outside the normalized request universe"
    ):
        await pipeline.run(WINDOW, resume=False)

    assert len(written) == 1
    assert pipeline.saved_plans == [{"bounded-keys": {"600519.SH": WINDOW}}]


async def test_reconstructed_key_outside_the_request_universe_fails_closed() -> None:
    fetched: list[str] = []

    def fetch(symbol: str, window: Window | None) -> pd.DataFrame:
        fetched.append(symbol)
        return _frame(symbol)

    pipeline = RecordingPipeline(
        _spec(["600519.SH"], source="rebuilt-keys", shard_size=5),
        session_maker=NO_SESSION,
        write_ods=lambda frame: len(frame),
        fetch_symbol=fetch,
        load_affected_keys=lambda context: [("000001.SZ", date(2026, 9, 22))],
        bounded_partitions=SymbolPartitioning(
            symbol_key_index=0, normalize_symbol=_plain, batch_size=2
        ),
    )

    # This leg is current through the end; only the ths side still has work.
    with pytest.raises(
        ValueError, match="reconstructed key symbol is outside the normalized request universe"
    ):
        await pipeline.run(
            WINDOW,
            resume=False,
            source_windows={"rebuilt-keys": {"600519.SH": None}, "ths": {"600519.SH": EARLIER}},
        )

    assert fetched == []
    assert pipeline.shard_records == [
        (0, ShardStatus.RUNNING, 0, None),
        # Nothing to fetch for this leg, so the shard lands as an empty done.
        (0, ShardStatus.DONE, 0, None),
    ]


async def test_bounded_run_spools_the_keys_that_belong_to_the_request() -> None:
    spooled: list[list[tuple[object, ...]]] = []

    async def cross_check(context: PipelineContext) -> None:
        spooled.append(list(context.affected_keys))

    pipeline = RecordingPipeline(
        _spec(["600519.SH", "000001.SZ"], source="bounded-keys", shard_size=5),
        session_maker=NO_SESSION,
        write_ods=lambda frame: len(frame),
        fetch_symbol=lambda symbol, window: _frame(symbol),
        cross_check=cross_check,
        bounded_partitions=SymbolPartitioning(
            symbol_key_index=0, normalize_symbol=_plain, batch_size=2
        ),
    )
    outcome = await pipeline.run(WINDOW)

    assert outcome.shards_done == 1
    assert outcome.failures == []
    assert pipeline.shard_records == [
        (0, ShardStatus.RUNNING, 0, None),
        (0, ShardStatus.DONE, 2, None),
    ]
    # The spool keeps the landed (source-spelled) keys and indexes them by
    # the normalized symbol, so the hook reads them back untouched.
    assert spooled == [
        [("000001.SZ", date(2026, 9, 22)), ("600519.SH", date(2026, 9, 22))],
    ]


async def test_reconstructed_keys_inside_the_request_are_spooled() -> None:
    spooled: list[list[tuple[object, ...]]] = []

    async def merge(context: PipelineContext) -> None:
        spooled.append(list(context.affected_keys))

    pipeline = RecordingPipeline(
        _spec(["600519.SH"], source="rebuilt-keys", shard_size=5),
        session_maker=NO_SESSION,
        write_ods=lambda frame: len(frame),
        fetch_symbol=lambda symbol, window: _frame(symbol),
        merge=merge,
        load_affected_keys=lambda context: [("600519.SH", date(2026, 9, 20))],
        bounded_partitions=SymbolPartitioning(
            symbol_key_index=0, normalize_symbol=_plain, batch_size=2
        ),
    )
    outcome = await pipeline.run(
        WINDOW,
        resume=False,
        source_windows={"rebuilt-keys": {"600519.SH": None}, "ths": {"600519.SH": EARLIER}},
    )

    # Nothing was fetched for this leg, yet the landed keys still reach the hook.
    assert outcome.rows_written == 0
    assert spooled == [[("600519.SH", date(2026, 9, 20))]]
    assert pipeline.step_records == [("merge", "running"), ("merge", "done")]


def test_bounded_key_processing_without_a_partition_configuration_fails_closed() -> None:
    pipeline = RecordingPipeline(
        _spec(["600519.SH"], source="legacy"),
        session_maker=NO_SESSION,
        write_ods=lambda frame: len(frame),
        fetch_symbol=lambda symbol, window: _frame(symbol),
    )

    assert pipeline._request_partitions(["600519.SH"]) == {}
    with pytest.raises(
        RuntimeError, match="bounded key processing has no symbol partition configuration"
    ):
        pipeline._require_bounded_partitions()


async def test_fetch_shard_drops_absent_and_empty_frames_but_keeps_failures() -> None:
    def fetch(symbol: str, window: Window) -> object:
        if symbol == "absent":
            return None
        if symbol == "empty":
            return pd.DataFrame()
        if symbol == "boom":
            raise RuntimeError("upstream 404")
        assert window == EARLIER
        return _frame(symbol)

    pipeline = RecordingPipeline(
        _spec(["ok"], source="fetch-shard"),
        session_maker=NO_SESSION,
        write_ods=lambda frame: len(frame),
        fetch_symbol=fetch,
    )

    frame, failures = await pipeline._fetch_shard(
        3,
        ["absent", "empty", "boom", "ok"],
        {"ok": EARLIER},
        WINDOW,
    )

    assert frame is not None
    assert list(frame["symbol"]) == ["ok"]
    assert failures == [ShardFailure(3, "boom", "RuntimeError: upstream 404")]
    nothing, nothing_failures = await pipeline._fetch_shard(0, ["absent", "empty"], {}, WINDOW)
    assert nothing is None
    assert nothing_failures == []


async def test_async_fetch_callbacks_are_awaited_per_symbol() -> None:
    seen: list[tuple[str, Window]] = []

    async def fetch(symbol: str, window: Window) -> pd.DataFrame:
        seen.append((symbol, window))
        return _frame(symbol)

    pipeline = RecordingPipeline(
        _spec(["600519"], source="fetch-shard"),
        session_maker=NO_SESSION,
        write_ods=lambda frame: len(frame),
        fetch_symbol=fetch,
    )

    frame, failures = await pipeline._fetch_shard(0, ["600519"], {}, WINDOW)

    assert failures == []
    assert frame is not None and list(frame["symbol"]) == ["600519"]
    # No symbol window means the run window is the fallback.
    assert seen == [("600519", WINDOW)]


def test_type_checking_only_declarations_resolve() -> None:
    """Execute the annotation-only block with the flag forced true.

    ``typing.TYPE_CHECKING`` is ``False`` at runtime, so the guarded
    imports and the ``FetchSymbol``/``WriteOds``/``Hook``/
    ``LoadAffectedKeys`` aliases never run; re-executing the source with
    the flag on proves they all bind to real objects.
    """
    namespace = _exec_with_type_checking(runner)

    assert namespace["Sequence"] is Sequence
    assert namespace["Awaitable"] is Awaitable
    assert namespace["MappingType"] is Mapping
    assert namespace["pd"] is pd
    assert namespace["AsyncSession"].__name__ == "AsyncSession"
    for alias in ("FetchSymbol", "WriteOds", "Hook", "LoadAffectedKeys"):
        assert typing.get_origin(namespace[alias]) is Callable


def _exec_with_type_checking(module: object) -> dict[str, object]:
    """Re-execute ``module``'s source with ``TYPE_CHECKING`` true.

    The probe runs under its own throwaway name: ``dataclass`` resolution
    looks the defining module up in ``sys.modules``, so the namespace has
    to be registered while the source executes (and removed after).
    """
    path = pathlib.Path(module.__file__)
    source = path.read_text(encoding="utf-8")
    name = f"opendata_c85_probe_{path.stem}"
    probe = types.ModuleType(name)
    probe.__dict__["__file__"] = str(path)
    original = typing.TYPE_CHECKING
    typing.TYPE_CHECKING = True
    sys.modules[name] = probe
    try:
        exec(compile(source, str(path), "exec"), probe.__dict__)
    finally:
        typing.TYPE_CHECKING = original
        sys.modules.pop(name, None)
    return dict(probe.__dict__)
