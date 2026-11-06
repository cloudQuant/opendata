"""AC15/D04 - the default THS replay invocation leaves no durable write.

202/AUDIT D04 was: ``backfill_ths_history.py`` advertised dry-run, then reached
``MarketDataStore``, which commits durable facts (and tries publication) inside
its own transaction, so the outer rollback could not undo it.  The 202 test gap
was a CLI case that mocked the runner and only checked the outer rollback.

These tests therefore never assert "no exception was raised".  They measure the
four durable layers AC15 names - ODS, DWD, publication, coverage - plus the
batch watermark and the pipeline checkpoints, through a *separate* database
connection before and after the default invocation, and they arm SQLite write
triggers on every one of those tables so a write aborts instead of going
unmeasured.  A paired control arm shows the measurement is not vacuous: the
identical sandbox with ``apply=True`` moves those same counters.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING

from sqlalchemy import text

from opendata.pipeline.watermark import WATERMARK_TABLE
from scripts.ops import replay_warehouse_pipeline as replay
from tests.ac15_ths_replay_fixture import (
    DWD_TABLE,
    ODS_TABLE,
    PROTECTED_TABLES,
    build_sandbox,
)

if TYPE_CHECKING:
    from pathlib import Path

    from pytest import MonkeyPatch

TARGET_ODS = f"count::target.{ODS_TABLE}"
TARGET_DWD = f"count::target.{DWD_TABLE}"
TARGET_WATERMARK = f"count::target.{WATERMARK_TABLE}"
SOURCE_ODS = f"count::source.{ODS_TABLE}"
PROGRESS = "count::metadata.pipeline_progress"
COVERAGE = "count::metadata.data_tables"
PUBLICATION_HEADS = "count::metadata.od_publication_heads"


def _layer_entries(state: dict[str, object]) -> dict[str, object]:
    """Return the snapshot entries that describe a durable layer."""
    return {
        key: value
        for key, value in state.items()
        if key.startswith("count::") or key.endswith("_rows") or key.startswith("head::")
    }


def test_dry_run_is_the_declared_default_of_the_tool() -> None:
    """The write gate defaults off in the call signature and in the CLI flag."""
    signature_default = inspect.signature(replay.run_replay).parameters["apply"].default
    assert signature_default is False
    apply_actions = [
        action for action in replay._parser()._actions if "--apply" in action.option_strings
    ]
    assert len(apply_actions) == 1
    assert apply_actions[0].default is False


def test_write_tripwires_really_block_every_measured_layer(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """Non-vacuity control: every protected table is live and every trigger bites."""
    sandbox = build_sandbox(tmp_path, monkeypatch)
    sandbox.arm_tripwires()
    refusals = {
        f"{label}.{table}": sandbox.attempt_forbidden_write(label, table)
        for label, table in PROTECTED_TABLES
    }
    assert len(refusals) == len(PROTECTED_TABLES)
    unblocked = [
        name for name, refusal in refusals.items() if "AC15 dry-run write tripwire" not in refusal
    ]
    assert unblocked == [], f"these layers were not actually guarded: {unblocked}"


def test_default_invocation_writes_nothing_to_ods_dwd_publication_or_coverage(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """D04: the default run is observed, from another connection, to be a no-op."""
    sandbox = build_sandbox(tmp_path, monkeypatch)
    sandbox.arm_tripwires()
    before = sandbox.snapshot()
    sandbox.engine_calls.clear()

    report = sandbox.run()

    assert report["status"] == "dry_run"
    assert report["record"] == "final"
    after = sandbox.snapshot()
    moved = {
        key: (before[key], after[key])
        for key, value in _layer_entries(before).items()
        if after[key] != value
    }
    assert moved == {}, "the default invocation wrote to a durable layer"
    # Heads are unchanged in value, not merely in row count.
    assert after["head::batch_watermark"] == before["head::batch_watermark"]
    assert after["head::publication_sequence"] == before["head::publication_sequence"]
    assert after["head::coverage_watermark"] == before["head::coverage_watermark"]
    # Structural plane: the default path never opens a write-capable engine.
    assert sandbox.engine_calls == ["source"]
    # The source snapshot it did read is intact.
    assert after[SOURCE_ODS] == before[SOURCE_ODS]


def test_the_dry_run_measurement_would_detect_a_write(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """Sensitivity control: the same comparison is not blind to a durable write.

    A "no writes" claim is only worth its detector, so one row is landed inside
    the target ODS between the two reads, exactly the way 202/D04's internal
    commit did, and the assertion machinery has to see it.
    """
    sandbox = build_sandbox(tmp_path, monkeypatch)
    before = sandbox.snapshot()
    sandbox.run()
    with sandbox.target_engine.begin() as connection:
        connection.execute(
            text(
                f"INSERT INTO `{ODS_TABLE}` (thscode, trade_date, open_price, high_price, "  # noqa: S608
                "low_price, close_price, volume, turnover, amount, date_ms, _source, "
                "_fetched_at, _batch_id) VALUES ('999999.SZ', '2024-01-02', 1, 1, 1, 1, 1, 1, "
                "1, NULL, 'ths', '2024-01-01 00:00:00', "
                "'22222222-2222-4222-8222-222222222222')"
            )
        )

    after = sandbox.snapshot()
    moved = {
        key: (before[key], after[key])
        for key, value in _layer_entries(before).items()
        if after[key] != value
    }
    assert moved, "the dry-run comparison cannot even see a write"
    assert f"count::target.{ODS_TABLE}" in moved


def test_control_apply_moves_exactly_the_layers_the_default_left_alone(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """Non-vacuity: the identical sandbox, with ``apply``, moves those counters."""
    sandbox = build_sandbox(tmp_path, monkeypatch)
    before = sandbox.snapshot()
    assert before[TARGET_ODS] == 0
    assert before[TARGET_DWD] == 0

    report = sandbox.run(apply=True, resume=False)

    assert report["status"] == "complete"
    after = sandbox.snapshot()
    expected_rows = len(sandbox.symbols) * len(sandbox.days)
    assert after[TARGET_ODS] == expected_rows
    assert after[TARGET_DWD] == expected_rows
    assert after[TARGET_WATERMARK] == before[TARGET_WATERMARK] + 1
    assert after["head::batch_watermark"] != before["head::batch_watermark"]
    assert after["head::coverage_watermark"] != before["head::coverage_watermark"]
    assert after[PROGRESS] > before[PROGRESS]
    assert after[COVERAGE] >= before[COVERAGE]
    # Publication is a separate durable layer; the replay path must not touch it
    # in either mode, which is why the default arm alone could not prove a thing.
    assert after[PUBLICATION_HEADS] == before[PUBLICATION_HEADS]
    assert after[SOURCE_ODS] == before[SOURCE_ODS]
