"""Offline boundary cases for fail-closed one-shot ledger row decoding."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

import opendata.pipeline.run_ledger as run_ledger

NOW = datetime(2026, 10, 26, 9, 30, 0, 123456, tzinfo=timezone.utc)
NOW_US = 1_793_007_000_123_456
FUTURE_US = NOW_US + 10_000_000


def _running_row() -> dict[str, object]:
    """Return a valid row shape from a claimed, one-shot ledger record."""
    return {
        "claim_policy": "one_shot",
        "status": "running",
        "fencing_token": 1,
        "worker": "worker-a",
        "lease_id": "0123456789abcdef0123456789abcdef",
        "lease_expires_at_us": FUTURE_US,
        "started_at_us": NOW_US,
        "heartbeat_at_us": NOW_US,
        "finished_at_us": None,
        "error_text": None,
    }


def _running_record() -> run_ledger.RunRecord:
    """Return a valid public snapshot from a claimed, one-shot ledger run."""
    return run_ledger.RunRecord(
        run_key="offline-one-shot",
        job_snapshot_json='{"dataset":"daily"}',
        status="running",
        worker="worker-a",
        lease_id="0123456789abcdef0123456789abcdef",
        fencing_token=1,
        lease_expires_at=NOW + timedelta(seconds=10),
        created_at=NOW,
        updated_at=NOW,
        started_at=NOW,
        heartbeat_at=NOW,
        finished_at=None,
        error_text=None,
        claim_policy="one_shot",
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"worker": None},
        {"worker": ""},
        {"lease_id": None},
        {"lease_id": "not-a-lease-id"},
        {"fencing_token": True},
        {"fencing_token": 0},
        {"started_at_us": None},
        {"started_at_us": -1},
        {"heartbeat_at_us": None},
        {"heartbeat_at_us": -1},
        {"finished_at_us": NOW_US},
        {"error_text": "previous error"},
        {"lease_expires_at_us": True},
        {"lease_expires_at_us": -1},
        {"lease_expires_at_us": NOW_US},
    ],
)
def test_row_uncertainty_guard_rejects_incomplete_or_expired_one_shot_state(
    changes: dict[str, object],
) -> None:
    """Treat malformed ownership metadata or an expired lease as uncertain."""
    row = _running_row()
    row.update(changes)

    assert run_ledger._row_is_uncertain_one_shot(row, NOW_US)


@pytest.mark.parametrize(
    "changes",
    [
        {"worker": None},
        {"worker": " "},
        {"lease_id": None},
        {"lease_id": "NOT-LOWERCASE-0123456789abcdef"},
        {"fencing_token": 0},
        {"started_at": None},
        {"started_at": datetime(1969, 12, 31, 23, 59, 59, tzinfo=timezone.utc)},
        {"heartbeat_at": None},
        {"heartbeat_at": datetime(1969, 12, 31, 23, 59, 59, tzinfo=timezone.utc)},
        {"finished_at": NOW},
        {"error_text": "previous error"},
        {"lease_expires_at": None},
        {"lease_expires_at": NOW},
    ],
)
def test_record_uncertainty_guard_rejects_incomplete_or_expired_one_shot_state(
    changes: dict[str, object],
) -> None:
    """Apply the same fail-closed rule to the immutable record representation."""
    record = replace(_running_record(), **changes)

    assert run_ledger._record_is_uncertain_one_shot(record, NOW_US)


def test_one_shot_uncertainty_respects_policy_and_lifecycle_state() -> None:
    """Only one-shot active rows are subject to the uncertain-outcome rule."""
    row = _running_row()
    assert not run_ledger._row_is_uncertain_one_shot(row, NOW_US)
    assert not run_ledger._row_is_uncertain_one_shot(
        {**row, "claim_policy": "recover_expired"}, NOW_US
    )
    assert not run_ledger._row_is_uncertain_one_shot({**row, "status": "succeeded"}, NOW_US)

    queued = {
        **row,
        "status": "queued",
        "fencing_token": 0,
        "worker": None,
        "lease_id": None,
        "lease_expires_at_us": None,
        "started_at_us": None,
        "heartbeat_at_us": None,
        "finished_at_us": None,
        "error_text": None,
    }
    assert not run_ledger._row_is_uncertain_one_shot(queued, NOW_US)
    assert run_ledger._row_is_uncertain_one_shot({**queued, "heartbeat_at_us": NOW_US}, NOW_US)

    record = _running_record()
    assert not run_ledger._record_is_uncertain_one_shot(record, NOW_US)
    assert not run_ledger._record_is_uncertain_one_shot(
        replace(record, claim_policy="recover_expired"), NOW_US
    )
    assert not run_ledger._record_is_uncertain_one_shot(replace(record, status="cancelled"), NOW_US)
    pristine_queued = replace(
        record,
        status="queued",
        worker=None,
        lease_id=None,
        fencing_token=0,
        lease_expires_at=None,
        started_at=None,
        heartbeat_at=None,
        finished_at=None,
    )
    assert not run_ledger._record_is_uncertain_one_shot(pristine_queued, NOW_US)
    assert run_ledger._record_is_uncertain_one_shot(
        replace(pristine_queued, error_text="stale diagnostic"), NOW_US
    )


def test_running_record_lease_expiry_decoder_requires_a_persisted_deadline() -> None:
    """Require a valid lease deadline before commit-time expiry arithmetic."""
    record = _running_record()

    assert run_ledger._record_lease_expiry_us(record) == FUTURE_US
    with pytest.raises(RuntimeError, match="no lease expiry"):
        run_ledger._record_lease_expiry_us(replace(record, lease_expires_at=None))


@pytest.mark.parametrize(
    "stored_policy",
    [None, "unknown-policy"],
)
def test_corrupt_persisted_claim_policy_is_rejected_during_row_decoding(
    stored_policy: object,
) -> None:
    """Reject unsupported stored policy values instead of silently defaulting."""
    with pytest.raises(RuntimeError, match="invalid claim policy"):
        run_ledger._as_claim_policy(stored_policy)

    assert run_ledger._as_claim_policy("one_shot") == "one_shot"
    assert run_ledger._as_claim_policy("recover_expired") == "recover_expired"


@pytest.mark.parametrize("policy", [True, "unknown-policy"])
def test_enqueue_rejects_non_string_or_unknown_claim_policy_before_storage(
    policy: object,
) -> None:
    """Validate caller policy values independently of any database connection."""
    with pytest.raises(ValueError, match="claim_policy"):
        run_ledger._resolve_claim_policy(policy)

    assert run_ledger._resolve_claim_policy(None) == "recover_expired"
    assert run_ledger._resolve_claim_policy("recover_expired") == "recover_expired"
    assert run_ledger._resolve_claim_policy("one_shot") == "one_shot"
