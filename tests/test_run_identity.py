"""Behavioral tests for the versioned pipeline run identity contract."""

from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from opendata.pipeline.run_identity import (
    RunRequest,
    ScheduleMetadata,
    TaskDefinition,
    build_run_identity,
)


def make_task(*, version: str = "v1", provider: str = "openbb") -> TaskDefinition:
    """Build a representative declared job for identity tests."""
    return TaskDefinition(
        job_id="daily-stock",
        version=version,
        dataset_id="stock.daily",
        provider=provider,
        model="equity_historical",
        timezone="Asia/Shanghai",
        calendar="SSE",
        schedule=ScheduleMetadata(
            kind="cron",
            expression="30 17 * * 1-5",
            description="weekday close refresh",
        ),
    )


def make_scheduled(
    occurrence: datetime,
    *,
    shard: str | None = "0/4",
    query: dict[str, object] | None = None,
    partition: dict[str, object] | None = None,
) -> RunRequest:
    """Build a representative scheduled request at the supplied aware instant."""
    return RunRequest(
        run_type="scheduled",
        query={"symbols": ["AAPL", "MSFT"], "adjustment": "split"} if query is None else query,
        partition={"trade_date": "2025-03-10"} if partition is None else partition,
        shard=shard,
        occurrence=occurrence,
    )


def test_scheduled_identity_matches_canonical_golden_vector() -> None:
    """Pin canonical bytes and the SHA-256 digest for a stable identity vector."""
    identity = build_run_identity(
        make_task(),
        make_scheduled(datetime(2025, 3, 10, 17, 0, tzinfo=ZoneInfo("Asia/Shanghai"))),
    )

    assert identity.canonical_json == (
        '{"contract_version":1,"job":{"dataset_id":"stock.daily",'
        '"job_id":"daily-stock","model":"equity_historical",'
        '"provider":"openbb","version":"v1"},"run":{"backfill_range":null,'
        '"business_request":{"partition":{"trade_date":"2025-03-10"},'
        '"query":{"adjustment":"split","symbols":["AAPL","MSFT"]}},'
        '"occurrence":"2025-03-10T09:00:00.000000Z","request_key_sha256":null,'
        '"run_type":"scheduled","shard":"0/4"}}'
    )
    assert identity.idempotency_key == (
        "a33bf7199363421c2e00bf8430fc60cd50f21d7675ac50853b8fdc7aa0eb462c"
    )
    assert identity.payload_hash == (
        "70d42aa9278e210fe123dd7ff0c85eec4e46a09c6afbd615baedd85b756a87e1"
    )


def test_equivalent_aware_timezones_normalize_to_the_same_identity() -> None:
    """Treat time-zone representations of the same instant as one occurrence."""
    task = make_task()

    shanghai_identity = build_run_identity(
        task,
        make_scheduled(datetime(2025, 3, 10, 17, 0, tzinfo=ZoneInfo("Asia/Shanghai"))),
    )
    utc_identity = build_run_identity(
        task,
        make_scheduled(datetime(2025, 3, 10, 9, 0, tzinfo=timezone.utc)),
    )

    assert shanghai_identity.idempotency_key == utc_identity.idempotency_key
    assert shanghai_identity.canonical_json == utc_identity.canonical_json


def test_slot_changes_with_job_version_shard_partition_and_occurrence() -> None:
    """Bind scheduled slots to job revision, shard, partition, and UTC instant."""
    base_request = make_scheduled(datetime(2025, 3, 10, 9, 0, tzinfo=timezone.utc))
    base = build_run_identity(make_task(), base_request).idempotency_key

    changed_version = build_run_identity(make_task(version="v2"), base_request).idempotency_key
    changed_shard = build_run_identity(
        make_task(),
        make_scheduled(datetime(2025, 3, 10, 9, 0, tzinfo=timezone.utc), shard="1/4"),
    ).idempotency_key
    changed_occurrence = build_run_identity(
        make_task(),
        make_scheduled(datetime(2025, 3, 10, 9, 1, tzinfo=timezone.utc)),
    ).idempotency_key
    changed_partition = build_run_identity(
        make_task(),
        make_scheduled(
            datetime(2025, 3, 10, 9, 0, tzinfo=timezone.utc),
            partition={"trade_date": "2025-03-11"},
        ),
    ).idempotency_key

    assert len({base, changed_version, changed_shard, changed_occurrence, changed_partition}) == 5


def test_scheduled_payload_drift_keeps_slot_key_and_changes_payload_hash() -> None:
    """Keep one scheduled slot when its provider query changes before claim."""
    task = make_task()
    occurrence = datetime(2025, 3, 10, 9, 0, tzinfo=timezone.utc)
    original = build_run_identity(task, make_scheduled(occurrence))
    changed_query = build_run_identity(
        task,
        make_scheduled(occurrence, query={"symbols": ["AAPL"], "adjustment": "split"}),
    )

    assert original.idempotency_key == changed_query.idempotency_key
    assert original.payload_hash != changed_query.payload_hash
    assert original.canonical_json != changed_query.canonical_json


def test_valid_nested_mutation_is_copied_into_the_new_payload_snapshot() -> None:
    """Revalidate current nested request data when a caller mutates it later."""
    task = make_task()
    request = make_scheduled(
        datetime(2025, 3, 10, 9, 0, tzinfo=timezone.utc),
        query={"symbols": ["AAPL"]},
    )
    original = build_run_identity(task, request)

    request.query["symbols"].append("MSFT")
    changed = build_run_identity(task, request)

    assert original.idempotency_key == changed.idempotency_key
    assert original.payload_hash != changed.payload_hash
    assert '"symbols":["AAPL","MSFT"]' in changed.canonical_json


def test_mutated_credentials_non_finite_values_and_cycles_are_rejected() -> None:
    """Recheck the mutable query tree before hashing or snapshot serialization."""
    task = make_task()
    occurrence = datetime(2025, 3, 10, 9, 0, tzinfo=timezone.utc)

    credential_request = make_scheduled(occurrence, query={"filters": {"symbols": []}})
    credential_request.query["filters"]["api_key"] = "mutated-secret"
    with pytest.raises(ValidationError, match="sensitive parameter name"):
        build_run_identity(task, credential_request)

    non_finite_request = make_scheduled(occurrence, query={"values": []})
    non_finite_request.query["values"].append(float("nan"))
    with pytest.raises(ValidationError, match="must be finite"):
        build_run_identity(task, non_finite_request)

    cyclic_values: list[object] = []
    cyclic_values.append(cyclic_values)
    cyclic_request = make_scheduled(occurrence, query={"values": []})
    cyclic_request.query["values"] = cyclic_values
    with pytest.raises(ValidationError, match="cyclic JSON value"):
        build_run_identity(task, cyclic_request)


def test_mutated_json_node_count_is_bounded_before_serialization() -> None:
    """Stop large shared nested containers before canonical JSON is built."""
    repeated_row = list(range(100))
    request = make_scheduled(
        datetime(2025, 3, 10, 9, 0, tzinfo=timezone.utc),
        query={"matrix": []},
    )
    request.query["matrix"] = [repeated_row] * 100

    with pytest.raises(ValidationError, match="maximum node count"):
        build_run_identity(make_task(), request)


def test_model_copy_update_cannot_bypass_job_validation() -> None:
    """Revalidate copied job models before their fields enter a run identity."""
    task = make_task().model_copy(update={"version": object()})
    request = make_scheduled(datetime(2025, 3, 10, 9, 0, tzinfo=timezone.utc))

    with pytest.raises(ValidationError):
        build_run_identity(task, request)

    copied_request = request.model_copy(update={"query": {"api_key": "copied-secret"}})
    with pytest.raises(ValidationError, match="sensitive parameter name"):
        build_run_identity(make_task(), copied_request)


def test_manual_requests_are_distinct_from_scheduled_slot_and_each_other() -> None:
    """Require request keys so same-day manual work cannot claim a natural slot."""
    task = make_task()
    scheduled = build_run_identity(
        task,
        make_scheduled(datetime(2025, 3, 10, 9, 0, tzinfo=timezone.utc)),
    )
    manual_one = build_run_identity(
        task,
        RunRequest(run_type="manual", idempotency_key="manual-20250310-001"),
    )
    manual_retry = build_run_identity(
        task,
        RunRequest(run_type="manual", idempotency_key="manual-20250310-001"),
    )
    manual_two = build_run_identity(
        task,
        RunRequest(run_type="manual", idempotency_key="manual-20250310-002"),
    )

    assert manual_one.idempotency_key == manual_retry.idempotency_key
    assert manual_one.idempotency_key != manual_two.idempotency_key
    assert manual_one.idempotency_key != scheduled.idempotency_key


def test_backfill_identity_includes_explicit_range_and_request_key() -> None:
    """Keep a backfill slot stable while its immutable range snapshot is checked."""
    task = make_task()
    first = RunRequest(
        run_type="backfill",
        idempotency_key="backfill-2025-q1",
        backfill_start=date(2025, 1, 1),
        backfill_end=date(2025, 3, 31),
    )
    same = RunRequest(
        run_type="backfill",
        idempotency_key="backfill-2025-q1",
        backfill_start=date(2025, 1, 1),
        backfill_end=date(2025, 3, 31),
    )
    changed_range = RunRequest(
        run_type="backfill",
        idempotency_key="backfill-2025-q1",
        backfill_start=date(2025, 1, 1),
        backfill_end=date(2025, 3, 30),
    )

    first_identity = build_run_identity(task, first)
    same_identity = build_run_identity(task, same)
    changed_identity = build_run_identity(task, changed_range)

    assert first_identity.idempotency_key == same_identity.idempotency_key
    assert first_identity.payload_hash == same_identity.payload_hash
    assert first_identity.idempotency_key == changed_identity.idempotency_key
    assert first_identity.payload_hash != changed_identity.payload_hash


def test_manual_payload_drift_keeps_explicit_request_slot() -> None:
    """Keep manual identity stable while changed payload receives a new hash."""
    task = make_task()
    original = build_run_identity(
        task,
        RunRequest(
            run_type="manual",
            idempotency_key="manual-20250310-001",
            query={"symbols": ["AAPL"]},
        ),
    )
    changed = build_run_identity(
        task,
        RunRequest(
            run_type="manual",
            idempotency_key="manual-20250310-001",
            query={"symbols": ["MSFT"]},
        ),
    )

    assert original.idempotency_key == changed.idempotency_key
    assert original.payload_hash != changed.payload_hash
    assert "manual-20250310-001" not in original.canonical_json


def test_task_schedule_metadata_is_retained_and_disabled_by_default() -> None:
    """Keep timezone, calendar, and schedule declarations without activating them."""
    task = make_task()

    assert task.enabled is False
    assert task.timezone == "Asia/Shanghai"
    assert task.calendar == "SSE"
    assert task.schedule is not None
    assert task.schedule.expression == "30 17 * * 1-5"


@pytest.mark.parametrize(
    "payload",
    [
        {"run_type": "manual"},
        {"run_type": "backfill", "idempotency_key": "backfill-1"},
        {
            "run_type": "backfill",
            "idempotency_key": "backfill-1",
            "backfill_start": date(2025, 3, 2),
            "backfill_end": date(2025, 3, 1),
        },
    ],
)
def test_manual_and_backfill_request_shapes_fail_closed(payload: dict[str, object]) -> None:
    """Reject missing idempotency keys, incomplete ranges, and inverted ranges."""
    with pytest.raises(ValidationError):
        RunRequest.model_validate(payload)


def test_naive_occurrence_is_rejected() -> None:
    """Reject local wall-clock occurrences that cannot identify an instant."""
    with pytest.raises(ValidationError, match="timezone-aware"):
        RunRequest(run_type="scheduled", occurrence=datetime(2025, 3, 10, 9, 0))


@pytest.mark.parametrize(
    "query",
    [
        {"value": float("nan")},
        {"value": float("inf")},
        {"api_key": "not-a-real-key"},
        {"value": "Bearer example-secret"},
        {"value": ("tuple", "is not JSON-native")},
        {"value": datetime(2025, 3, 10, tzinfo=timezone.utc)},
    ],
)
def test_query_rejects_non_json_non_finite_and_credential_values(
    query: dict[str, object],
) -> None:
    """Reject values that cannot safely be serialized as bounded public JSON."""
    with pytest.raises(ValidationError):
        RunRequest(run_type="manual", idempotency_key="manual-1", query=query)


def test_query_size_and_depth_are_bounded() -> None:
    """Bound nested and encoded query size before identity generation."""
    oversized_text = "x" * 2_049
    deeply_nested: object = "leaf"
    for _ in range(10):
        deeply_nested = [deeply_nested]

    with pytest.raises(ValidationError, match="2048 characters"):
        RunRequest(run_type="manual", idempotency_key="manual-1", query={"value": oversized_text})
    with pytest.raises(ValidationError, match="maximum depth"):
        RunRequest(run_type="manual", idempotency_key="manual-1", query={"value": deeply_nested})


def test_unsupported_fields_and_arbitrary_commands_are_rejected() -> None:
    """Reject undeclared job fields, including command-like execution inputs."""
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        TaskDefinition.model_validate(
            {
                "job_id": "daily-stock",
                "version": "v1",
                "dataset_id": "stock.daily",
                "provider": "openbb",
                "model": "equity_historical",
                "command": "python arbitrary.py",
            }
        )


def test_invalid_timezone_is_rejected() -> None:
    """Reject schedule metadata whose time-zone name cannot be resolved."""
    with pytest.raises(ValidationError, match="IANA time-zone"):
        TaskDefinition(
            job_id="daily-stock",
            version="v1",
            dataset_id="stock.daily",
            provider="openbb",
            model="equity_historical",
            timezone="Not/AZone",
        )


def test_enabled_task_requires_schedule_metadata() -> None:
    """Prevent a job from enabling scheduled execution without a schedule."""
    with pytest.raises(ValidationError, match="require schedule metadata"):
        TaskDefinition(
            job_id="daily-stock",
            version="v1",
            dataset_id="stock.daily",
            provider="openbb",
            model="equity_historical",
            enabled=True,
        )
