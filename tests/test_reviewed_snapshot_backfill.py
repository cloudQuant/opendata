"""Behavioral tests for caller-supplied instrument snapshot planning."""

from __future__ import annotations

import hashlib
from datetime import date
from typing import get_type_hints

import pytest

from opendata.data.instrument_reference import (
    InstrumentAlias,
    InstrumentLifecycle,
    InstrumentReferenceRegistry,
    LifecycleStatus,
)
from opendata.data.instrument_reference_snapshot import (
    InstrumentReferenceSnapshotError,
    encode_instrument_reference_snapshot,
)
from opendata.data.platform_contracts import DatasetContract, FieldContract, SourceIdentity
from opendata.data.providers.akshare._source import SOURCE
from opendata.pipeline.reviewed_snapshot_backfill import (
    SnapshotStockDailyBackfillCandidate,
    build_stock_daily_backfill_plan_from_snapshot,
)
from opendata.pipeline.run_identity import TaskDefinition
from opendata.pipeline.stock_daily_backfill_plan import (
    BackfillBlockReason,
    StockDailyBackfillPlan,
)

_DATASET_ID = "cn-equity-stock-daily"
_SOURCE_ID = "akshare.daily"
_AS_OF = date(2026, 10, 27)
_START = date(2020, 1, 1)


def _contract() -> DatasetContract:
    """Build the exact raw daily-bar contract used by stock_daily."""
    return DatasetContract(
        dataset_id=_DATASET_ID,
        schema_version="1.0.0",
        semantic_version="stock-daily-raw-v1",
        asset_class="equity",
        frequency="1d",
        record_kind="time_series",
        fields=(
            FieldContract(name="market", data_type="string"),
            FieldContract(name="symbol", data_type="string"),
            FieldContract(name="trade_date", data_type="date"),
            FieldContract(name="open", data_type="number"),
            FieldContract(name="high", data_type="number"),
            FieldContract(name="low", data_type="number"),
            FieldContract(name="close", data_type="number"),
            FieldContract(name="volume", data_type="number"),
            FieldContract(name="amount", data_type="number"),
        ),
        business_key=("market", "symbol", "trade_date"),
        ordering=("trade_date", "market", "symbol"),
        filter_fields=("market", "symbol"),
        time_field="trade_date",
        temporal_semantics="business_date",
        source=SourceIdentity(source_id=_SOURCE_ID, source=SOURCE),
        field_units={
            "open": "CNY/share",
            "high": "CNY/share",
            "low": "CNY/share",
            "close": "CNY/share",
            "volume": "share",
            "amount": "CNY",
        },
        field_currencies={
            "open": "CNY",
            "high": "CNY",
            "low": "CNY",
            "close": "CNY",
            "amount": "CNY",
        },
        currency="CNY",
        adjustment_basis="raw",
        field_adjustments={
            "open": "raw",
            "high": "raw",
            "low": "raw",
            "close": "raw",
            "volume": "not_applicable",
            "amount": "not_applicable",
        },
        calendar_id="SSE",
        timezone="Asia/Shanghai",
        pit_policy="unsupported",
    )


def _task() -> TaskDefinition:
    """Build the existing disabled stock-daily task declaration."""
    return TaskDefinition(
        job_id="cn-stock-daily-backfill-fixture",
        version="v1",
        dataset_id=_DATASET_ID,
        provider=SOURCE,
        model="stock_daily",
        schedule=None,
        enabled=False,
    )


def _lifecycle(
    instrument_id: str,
    *,
    dataset_id: str = _DATASET_ID,
    source_id: str = _SOURCE_ID,
    status: LifecycleStatus = LifecycleStatus.ACTIVE,
    known_start_date: date | None = _START,
    start_boundary_unknown: bool = False,
) -> InstrumentLifecycle:
    """Build one synthetic lifecycle with explicit boundary semantics."""
    return InstrumentLifecycle(
        dataset_id=dataset_id,
        source_id=source_id,
        instrument_id=instrument_id,
        lifecycle_version=f"life-{instrument_id}",
        status=status,
        status_as_of=_AS_OF,
        status_evidence_ref=f"TEST:status:{instrument_id}",
        known_start_date=known_start_date,
        start_boundary_unknown=start_boundary_unknown,
        start_evidence_ref=(None if start_boundary_unknown else f"TEST:listing:{instrument_id}"),
    )


def _alias(instrument_id: str, symbol: str) -> InstrumentAlias:
    """Build one synthetic source alias covering the explicit date interval."""
    return InstrumentAlias(
        dataset_id=_DATASET_ID,
        source_id=_SOURCE_ID,
        alias=symbol,
        instrument_id=instrument_id,
        alias_version=f"alias-{instrument_id}",
        evidence_ref=f"TEST:alias:{instrument_id}",
        valid_from=_START,
        valid_through=_AS_OF,
    )


def _registry(
    lifecycles: tuple[InstrumentLifecycle, ...],
    aliases: tuple[InstrumentAlias, ...],
) -> InstrumentReferenceRegistry:
    """Build an immutable synthetic registry for codec-backed tests."""
    return InstrumentReferenceRegistry(
        registry_version="TEST:registry-v1",
        evidence_ref="TEST:review-record",
        lifecycles=lifecycles,
        aliases=aliases,
    )


def _plan(snapshot_bytes: bytes) -> SnapshotStockDailyBackfillCandidate:
    """Call the adapter with explicit task, contract, and cutoff inputs."""
    return build_stock_daily_backfill_plan_from_snapshot(
        snapshot_bytes,
        task=_task(),
        contract=_contract(),
        as_of=_AS_OF,
    )


def test_public_api_type_hints_resolve_for_runtime_introspection() -> None:
    """Keep callable and result annotations usable by runtime API tooling."""
    function_hints = get_type_hints(build_stock_daily_backfill_plan_from_snapshot)
    result_hints = get_type_hints(SnapshotStockDailyBackfillCandidate)

    assert function_hints == {
        "snapshot_bytes": bytes,
        "task": TaskDefinition,
        "contract": DatasetContract,
        "as_of": date,
        "max_window_days": int,
        "return": SnapshotStockDailyBackfillCandidate,
    }
    assert result_hints == {
        "snapshot_bytes_sha256": str,
        "plan": StockDailyBackfillPlan,
    }


def test_mixed_scope_snapshot_retains_exact_plan_scope_and_distinct_digest_fields() -> None:
    """Select only exact dataset/source rows and keep raw and plan pins separate."""
    target = _lifecycle("CN:600519")
    other_dataset = _lifecycle("CN:000001", dataset_id="other-dataset")
    other_source = _lifecycle("CN:000002", source_id="other-source")
    snapshot = encode_instrument_reference_snapshot(
        _registry(
            (target, other_dataset, other_source),
            (_alias("CN:600519", "600519.SH"),),
        )
    )

    result = _plan(snapshot)
    repeated = _plan(snapshot)

    assert result.snapshot_bytes_sha256 == hashlib.sha256(snapshot).hexdigest()
    assert result.snapshot_bytes_sha256 == repeated.snapshot_bytes_sha256
    assert result.plan.plan_sha256 == repeated.plan.plan_sha256
    assert result.plan.registry_lifecycle_count == 1
    assert result.plan.registry_version == "TEST:registry-v1"
    assert result.plan.registry_evidence_ref == "TEST:review-record"
    assert [item.instrument_id for item in result.plan.instruments] == ["CN:600519"]
    assert result.plan.candidate_chunks
    assert result.plan.execution_authorized is False
    assert result.plan.full_history_ready is False
    assert result.plan.universe_scope_complete is False
    assert result.plan.source_history_limit == "unknown"


def test_unknown_start_is_preserved_and_never_generates_candidate_chunks() -> None:
    """Keep an unknown lower boundary as a blocker without deriving a date."""
    lifecycle = _lifecycle(
        "CN:600519",
        known_start_date=None,
        start_boundary_unknown=True,
    )
    result = _plan(
        encode_instrument_reference_snapshot(
            _registry((lifecycle,), (_alias("CN:600519", "600519.SH"),))
        )
    )
    instrument = result.plan.instruments[0]

    assert instrument.known_start_date is None
    assert instrument.start_boundary_unknown is True
    assert BackfillBlockReason.UNKNOWN_START_BOUNDARY in {
        blocker.reason for blocker in instrument.blockers
    }
    assert instrument.candidate_chunks == ()
    assert result.plan.execution_authorized is False
    assert result.plan.full_history_ready is False


def test_unknown_lifecycle_status_remains_a_blocker_even_with_candidate_chunks() -> None:
    """Retain status uncertainty and the global blockers when a range is constructible."""
    lifecycle = _lifecycle("CN:600519", status=LifecycleStatus.UNKNOWN)
    result = _plan(
        encode_instrument_reference_snapshot(
            _registry((lifecycle,), (_alias("CN:600519", "600519.SH"),))
        )
    )

    assert result.plan.instruments[0].candidate_chunks
    assert BackfillBlockReason.UNKNOWN_LIFECYCLE_STATUS in {
        blocker.reason for blocker in result.plan.instruments[0].blockers
    }
    assert BackfillBlockReason.UNIVERSE_SCOPE_UNATTESTED in {
        blocker.reason for blocker in result.plan.blockers
    }
    assert result.plan.execution_authorized is False
    assert result.plan.full_history_ready is False


def test_snapshot_codec_rejects_tampered_internal_content_hash() -> None:
    """A changed document fails its own content digest even when bytes are supplied."""
    snapshot = encode_instrument_reference_snapshot(
        _registry((_lifecycle("CN:600519"),), (_alias("CN:600519", "600519.SH"),))
    )
    tampered = snapshot.replace(b"TEST:review-record", b"TEST:changed-record")

    with pytest.raises(InstrumentReferenceSnapshotError, match="content_sha256"):
        _plan(tampered)


def test_snapshot_without_exact_task_source_lifecycle_fails_closed() -> None:
    """Do not reinterpret off-scope lifecycle rows as stock-daily planner inputs."""
    snapshot = encode_instrument_reference_snapshot(
        _registry(
            (_lifecycle("CN:600519", dataset_id="other-dataset"),),
            (),
        )
    )

    with pytest.raises(ValueError, match="no lifecycle rows"):
        _plan(snapshot)


def test_non_bytes_input_is_rejected_without_coercion() -> None:
    """The codec rejects mutable bytes-like and text data rather than coercing it."""
    with pytest.raises(InstrumentReferenceSnapshotError, match="must be bytes"):
        _plan(bytearray(b"{}"))  # type: ignore[arg-type]
