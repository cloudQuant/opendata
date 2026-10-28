"""Behavioral tests for the injected-engine pipeline run ledger."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import Engine, create_engine, update
from sqlalchemy.exc import OperationalError

from opendata.pipeline.run_ledger import (
    RUN_LEDGER_METADATA,
    RUN_LEDGER_TABLE,
    RunExecutionUncertainError,
    RunLedger,
    RunLedgerConflictError,
)

if TYPE_CHECKING:
    from pathlib import Path

NOW = datetime(2026, 10, 26, 9, 30, 0, 123456, tzinfo=timezone.utc)
SNAPSHOT = '{"dataset":"daily","symbol":"600519"}'


def _database_engine(path: Path) -> Engine:
    """Create an independent SQLite connection pool for one test database."""
    return create_engine(f"sqlite:///{path}", connect_args={"timeout": 15})


@pytest.fixture
def ledger_setup(tmp_path: Path) -> tuple[Path, Engine, RunLedger]:
    """Provide a file-backed ledger whose schema creation stays test-owned."""
    path = tmp_path / "run-ledger.sqlite"
    engine = _database_engine(path)
    RUN_LEDGER_METADATA.create_all(engine)
    ledger = RunLedger(engine)
    yield path, engine, ledger
    engine.dispose()


def test_enqueue_is_idempotent_and_rejects_snapshot_drift(ledger_setup) -> None:
    _, _, ledger = ledger_setup

    first = ledger.enqueue("daily:2026-10-26", SNAPSHOT)
    repeated = ledger.enqueue("daily:2026-10-26", SNAPSHOT)

    assert repeated == first
    assert first.status == "queued"
    assert first.fencing_token == 0
    assert first.lease_id is None
    assert first.job_snapshot_json == SNAPSHOT
    with pytest.raises(RunLedgerConflictError, match="different job snapshot"):
        ledger.enqueue("daily:2026-10-26", '{"dataset":"daily","symbol":"000001"}')
    assert ledger.get("daily:2026-10-26") == first


def test_enqueue_policy_is_explicit_immutable_and_defaults_to_recovery(ledger_setup) -> None:
    _, _, ledger = ledger_setup

    legacy = ledger.enqueue("legacy-policy", SNAPSHOT)
    one_shot = ledger.enqueue("one-shot-policy", SNAPSHOT, claim_policy="one_shot")

    assert legacy.claim_policy == "recover_expired"
    assert one_shot.claim_policy == "one_shot"
    assert ledger.enqueue("one-shot-policy", SNAPSHOT, claim_policy="one_shot") == one_shot
    with pytest.raises(RunLedgerConflictError, match="different claim policy"):
        ledger.enqueue("one-shot-policy", SNAPSHOT)
    with pytest.raises(RunLedgerConflictError, match="different claim policy"):
        ledger.enqueue("legacy-policy", SNAPSHOT, claim_policy="one_shot")
    with pytest.raises(ValueError, match="claim_policy"):
        ledger.enqueue("invalid-policy", SNAPSHOT, claim_policy="retry")
    assert ledger.get("invalid-policy") is None


def test_generic_claim_skips_one_shot_runs_even_after_lease_expiry(ledger_setup) -> None:
    _, _, ledger = ledger_setup
    one_shot = ledger.enqueue("one-shot-isolation", SNAPSHOT, claim_policy="one_shot")
    recoverable = ledger.enqueue("recoverable-run", SNAPSHOT)

    generic_claim = ledger.claim("worker", NOW, timedelta(seconds=2))

    assert generic_claim is not None
    assert generic_claim.run_key == recoverable.run_key
    assert generic_claim.claim_policy == "recover_expired"
    assert (
        ledger.finish(
            generic_claim.run_key,
            "worker",
            generic_claim.fencing_token,
            NOW + timedelta(seconds=1),
        )
        is not None
    )
    assert ledger.claim("worker", NOW + timedelta(seconds=3), timedelta(seconds=2)) is None
    assert ledger.get(one_shot.run_key) == one_shot


def test_claim_run_claims_only_first_one_shot_attempt(ledger_setup) -> None:
    _, _, ledger = ledger_setup
    queued = ledger.enqueue("exact-one-shot", SNAPSHOT, claim_policy="one_shot")

    assert ledger.claim("generic-worker", NOW, timedelta(seconds=10)) is None
    assert ledger.claim_run("exact-one-shot", "worker", NOW, timedelta(seconds=10)) is not None
    claimed = ledger.get(queued.run_key)

    assert claimed is not None
    assert claimed.status == "running"
    assert claimed.claim_policy == "one_shot"
    assert claimed.started_at == NOW
    assert claimed.fencing_token == 1
    assert claimed.lease_id is not None
    assert ledger.claim_run("exact-one-shot", "second-worker", NOW, timedelta(seconds=10)) is None
    assert (
        ledger.claim("generic-worker", NOW + timedelta(seconds=11), timedelta(seconds=10)) is None
    )


def test_expired_one_shot_run_is_uncertain_and_never_reclaimed(ledger_setup) -> None:
    _, _, ledger = ledger_setup
    ledger.enqueue("uncertain-one-shot", SNAPSHOT, claim_policy="one_shot")
    claim = ledger.claim_run("uncertain-one-shot", "worker", NOW, timedelta(seconds=2))
    assert claim is not None

    with pytest.raises(RunExecutionUncertainError) as first_error:
        ledger.claim_run(
            "uncertain-one-shot", "retry-worker", NOW + timedelta(seconds=2), timedelta(seconds=2)
        )
    assert first_error.value.run_key == claim.run_key
    assert first_error.value.outcome == "uncertain"
    assert ledger.get(claim.run_key) == claim

    with pytest.raises(RunExecutionUncertainError):
        ledger.claim_run(
            "uncertain-one-shot", "later-worker", NOW + timedelta(days=1), timedelta(seconds=2)
        )
    assert ledger.claim("generic-worker", NOW + timedelta(days=1), timedelta(seconds=2)) is None
    assert ledger.get(claim.run_key) == claim


def test_claim_run_fails_closed_for_cancelled_terminal_and_legacy_rows(ledger_setup) -> None:
    _, _, ledger = ledger_setup
    legacy = ledger.enqueue("legacy-exact-claim", SNAPSHOT)
    cancelled = ledger.enqueue("cancelled-one-shot", SNAPSHOT, claim_policy="one_shot")
    terminal = ledger.enqueue("terminal-one-shot", SNAPSHOT, claim_policy="one_shot")

    assert ledger.claim_run(legacy.run_key, "worker", NOW, timedelta(seconds=10)) is None
    assert ledger.cancel(cancelled.run_key, NOW) is not None
    running = ledger.claim_run(terminal.run_key, "worker", NOW, timedelta(seconds=10))
    assert running is not None
    finished = ledger.finish(
        terminal.run_key, "worker", running.fencing_token, NOW + timedelta(seconds=1)
    )
    assert finished is not None
    assert ledger.claim_run(cancelled.run_key, "worker", NOW, timedelta(seconds=10)) is None
    assert ledger.claim_run(terminal.run_key, "worker", NOW, timedelta(seconds=10)) is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("fencing_token", 1),
        ("worker", "stale-worker"),
        ("lease_id", "stale-lease"),
        ("lease_expires_at_us", 1),
        ("started_at_us", 1),
        ("heartbeat_at_us", 1),
        ("finished_at_us", 1),
        ("error_text", "previous failure"),
    ],
)
def test_claim_run_marks_noninitial_queued_one_shot_state_uncertain(
    ledger_setup, field: str, value: object
) -> None:
    _, engine, ledger = ledger_setup
    queued = ledger.enqueue("noninitial-one-shot", SNAPSHOT, claim_policy="one_shot")
    with engine.begin() as connection:
        connection.execute(
            update(RUN_LEDGER_TABLE)
            .where(RUN_LEDGER_TABLE.c.run_key == queued.run_key)
            .values(**{field: value})
        )

    before_claim = ledger.get(queued.run_key)
    assert before_claim is not None
    with pytest.raises(RunExecutionUncertainError) as error:
        ledger.claim_run(queued.run_key, "worker", NOW, timedelta(seconds=10))

    assert error.value.outcome == "uncertain"
    assert error.value.run_key == queued.run_key
    assert ledger.get(queued.run_key) == before_claim


def test_expired_one_shot_with_missing_lease_deadline_is_uncertain(ledger_setup) -> None:
    _, engine, ledger = ledger_setup
    queued = ledger.enqueue("one-shot-missing-deadline", SNAPSHOT, claim_policy="one_shot")
    claim = ledger.claim_run(queued.run_key, "worker", NOW, timedelta(seconds=10))
    assert claim is not None
    with engine.begin() as connection:
        connection.execute(
            update(RUN_LEDGER_TABLE)
            .where(RUN_LEDGER_TABLE.c.run_key == queued.run_key)
            .values(lease_expires_at_us=None)
        )

    before_claim = ledger.get(queued.run_key)
    assert before_claim is not None
    assert before_claim.status == "running"
    assert before_claim.lease_expires_at is None
    with pytest.raises(RunExecutionUncertainError) as error:
        ledger.claim_run(queued.run_key, "retry-worker", NOW, timedelta(seconds=10))

    assert error.value.outcome == "uncertain"
    assert error.value.run_key == queued.run_key
    assert ledger.get(queued.run_key) == before_claim


def test_two_independent_connections_compete_for_one_one_shot_run_with_one_winner(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "shared-one-shot-ledger.sqlite"
    setup_engine = _database_engine(database_path)
    RUN_LEDGER_METADATA.create_all(setup_engine)
    setup_ledger = RunLedger(setup_engine)
    setup_ledger.enqueue("one-shot-slot", SNAPSHOT, claim_policy="one_shot")

    first_engine = _database_engine(database_path)
    second_engine = _database_engine(database_path)
    first_ledger = RunLedger(first_engine)
    second_ledger = RunLedger(second_engine)
    barrier = Barrier(2)

    def claim(ledger: RunLedger, worker: str):
        barrier.wait(timeout=5)
        return ledger.claim_run("one-shot-slot", worker, NOW, timedelta(seconds=30))

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = (
                pool.submit(claim, first_ledger, "worker-a"),
                pool.submit(claim, second_ledger, "worker-b"),
            )
            results = [future.result(timeout=10) for future in futures]
    finally:
        first_engine.dispose()
        second_engine.dispose()
        setup_engine.dispose()

    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert winners[0].run_key == "one-shot-slot"
    assert winners[0].claim_policy == "one_shot"
    assert winners[0].fencing_token == 1


def test_claim_run_rejects_invalid_arguments_before_ledger_transition(ledger_setup) -> None:
    _, _, ledger = ledger_setup
    queued = ledger.enqueue("claim-run-validation", SNAPSHOT, claim_policy="one_shot")

    with pytest.raises(ValueError, match="run_key must be a non-empty"):
        ledger.claim_run(" ", "worker", NOW, timedelta(seconds=1))
    with pytest.raises(ValueError, match="worker must be a non-empty"):
        ledger.claim_run(queued.run_key, " ", NOW, timedelta(seconds=1))
    with pytest.raises(ValueError, match="timezone-aware"):
        ledger.claim_run(queued.run_key, "worker", NOW.replace(tzinfo=None), timedelta(seconds=1))
    with pytest.raises(ValueError, match="ttl"):
        ledger.claim_run(queued.run_key, "worker", NOW, timedelta(0))

    assert ledger.get(queued.run_key) == queued


def test_claim_returns_one_distinct_task_per_call(ledger_setup) -> None:
    _, _, ledger = ledger_setup
    for run_key in ("run-c", "run-a", "run-b"):
        ledger.enqueue(run_key, SNAPSHOT)

    claims = [ledger.claim("worker-a", NOW, timedelta(seconds=10)) for _ in range(3)]

    assert all(claim is not None for claim in claims)
    assert len({claim.run_key for claim in claims if claim is not None}) == 3
    assert all(claim.status == "running" for claim in claims if claim is not None)
    assert all(claim.fencing_token == 1 for claim in claims if claim is not None)
    assert all(claim.worker == "worker-a" for claim in claims if claim is not None)
    assert all(claim.lease_expires_at == NOW + timedelta(seconds=10) for claim in claims if claim)
    assert ledger.claim("worker-a", NOW, timedelta(seconds=10)) is None


def test_two_independent_connections_compete_for_one_run_with_one_winner(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "shared-ledger.sqlite"
    setup_engine = _database_engine(database_path)
    RUN_LEDGER_METADATA.create_all(setup_engine)
    setup_ledger = RunLedger(setup_engine)
    setup_ledger.enqueue("one-slot", SNAPSHOT)

    first_engine = _database_engine(database_path)
    second_engine = _database_engine(database_path)
    first_ledger = RunLedger(first_engine)
    second_ledger = RunLedger(second_engine)
    barrier = Barrier(2)

    def claim(ledger: RunLedger, worker: str):
        barrier.wait(timeout=5)
        return ledger.claim(worker, NOW, timedelta(seconds=30))

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = (
                pool.submit(claim, first_ledger, "worker-a"),
                pool.submit(claim, second_ledger, "worker-b"),
            )
            results = [future.result(timeout=10) for future in futures]
    finally:
        first_engine.dispose()
        second_engine.dispose()
        setup_engine.dispose()

    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert winners[0].run_key == "one-slot"
    assert winners[0].fencing_token == 1
    assert winners[0].worker in {"worker-a", "worker-b"}


def test_expired_lease_reclaim_fences_old_worker_and_survives_reopen(
    ledger_setup,
) -> None:
    path, engine, ledger = ledger_setup
    ledger.enqueue("recover-me", SNAPSHOT)
    old_claim = ledger.claim("worker-old", NOW, timedelta(seconds=5))
    assert old_claim is not None
    assert old_claim.lease_id is not None
    engine.dispose()

    reopened_engine = _database_engine(path)
    reopened_ledger = RunLedger(reopened_engine)
    try:
        assert (
            reopened_ledger.claim("worker-early", NOW + timedelta(seconds=4), timedelta(seconds=5))
            is None
        )
        renewed = reopened_ledger.heartbeat(
            old_claim.run_key,
            "worker-old",
            old_claim.fencing_token,
            NOW + timedelta(seconds=4),
            timedelta(seconds=8),
        )
        assert renewed is not None
        assert renewed.lease_expires_at == NOW + timedelta(seconds=12)

        reclaimed = reopened_ledger.claim(
            "worker-new", NOW + timedelta(seconds=12), timedelta(seconds=20)
        )
        assert reclaimed is not None
        assert reclaimed.fencing_token == old_claim.fencing_token + 1
        assert reclaimed.worker == "worker-new"
        assert reclaimed.lease_id != old_claim.lease_id
        assert (
            reopened_ledger.heartbeat(
                old_claim.run_key,
                "worker-old",
                old_claim.fencing_token,
                NOW + timedelta(seconds=13),
                timedelta(seconds=10),
            )
            is None
        )
        assert (
            reopened_ledger.finish(
                old_claim.run_key,
                "worker-old",
                old_claim.fencing_token,
                NOW + timedelta(seconds=13),
            )
            is None
        )
        completed = reopened_ledger.finish(
            reclaimed.run_key,
            "worker-new",
            reclaimed.fencing_token,
            NOW + timedelta(seconds=13),
        )
        assert completed is not None
        assert completed.status == "succeeded"
        assert completed.finished_at == NOW + timedelta(seconds=13)
        assert (
            reopened_ledger.claim("worker-late", NOW + timedelta(seconds=14), timedelta(seconds=10))
            is None
        )
    finally:
        reopened_engine.dispose()


def test_cancel_revokes_current_token_and_terminal_state_is_immutable(ledger_setup) -> None:
    _, _, ledger = ledger_setup
    ledger.enqueue("cancel-me", SNAPSHOT)
    claim = ledger.claim("worker", NOW, timedelta(seconds=10))
    assert claim is not None

    cancelled = ledger.cancel(claim.run_key, NOW + timedelta(seconds=1))

    assert cancelled is not None
    assert cancelled.status == "cancelled"
    assert cancelled.fencing_token == claim.fencing_token + 1
    assert cancelled.worker is None
    assert cancelled.lease_id is None
    assert cancelled.lease_expires_at is None
    assert (
        ledger.heartbeat(
            claim.run_key,
            "worker",
            claim.fencing_token,
            NOW + timedelta(seconds=2),
            timedelta(seconds=10),
        )
        is None
    )
    assert (
        ledger.finish(
            claim.run_key,
            "worker",
            claim.fencing_token,
            NOW + timedelta(seconds=2),
        )
        is None
    )
    assert ledger.cancel(claim.run_key, NOW + timedelta(seconds=3)) == cancelled
    assert ledger.get(claim.run_key) == cancelled


def test_cancel_queued_run_prevents_a_future_claim(ledger_setup) -> None:
    _, _, ledger = ledger_setup
    queued = ledger.enqueue("cancel-before-claim", SNAPSHOT)

    cancelled = ledger.cancel(queued.run_key, NOW)

    assert cancelled is not None
    assert cancelled.status == "cancelled"
    assert cancelled.fencing_token == 1
    assert cancelled.finished_at == NOW
    assert ledger.claim("worker", NOW, timedelta(seconds=10)) is None


def test_failure_is_terminal_and_preserves_error(ledger_setup) -> None:
    _, _, ledger = ledger_setup
    ledger.enqueue("fail-me", SNAPSHOT)
    claim = ledger.claim("worker", NOW, timedelta(seconds=10))
    assert claim is not None

    failed = ledger.fail(
        claim.run_key,
        "worker",
        claim.fencing_token,
        NOW + timedelta(seconds=1),
        "provider returned unavailable",
    )

    assert failed is not None
    assert failed.status == "failed"
    assert failed.error_text == "provider returned unavailable"
    assert (
        ledger.finish(
            claim.run_key,
            "worker",
            claim.fencing_token,
            NOW + timedelta(seconds=2),
        )
        is None
    )
    assert ledger.cancel(claim.run_key, NOW + timedelta(seconds=2)) == failed


@pytest.mark.parametrize(
    "snapshot",
    [
        "[]",
        '{"b":2, "a":1}',
        '{"a":1,"a":2}',
        '{"value":NaN}',
        '{"nested":{"api_key":"private"}}',
        '{"steps":[{"access_token":"private"}]}',
    ],
)
def test_enqueue_rejects_noncanonical_or_unsafe_snapshots(ledger_setup, snapshot) -> None:
    _, _, ledger = ledger_setup
    with pytest.raises(ValueError):
        ledger.enqueue("invalid-snapshot", snapshot)
    assert ledger.get("invalid-snapshot") is None


def test_enqueue_rejects_snapshot_over_limit_and_run_key_is_bound(ledger_setup) -> None:
    _, _, ledger = ledger_setup
    oversized_snapshot = '{"value":"' + ("x" * 1_048_576) + '"}'
    with pytest.raises(ValueError, match="exceeds"):
        ledger.enqueue("too-large", oversized_snapshot)

    injection_like_key = "slot' OR 1=1 --"
    created = ledger.enqueue(injection_like_key, SNAPSHOT)
    assert ledger.get(injection_like_key) == created
    assert ledger.get("other-slot") is None


@pytest.mark.parametrize("run_key", [" leading", "trailing ", " both "])
def test_enqueue_rejects_run_keys_with_outer_whitespace(ledger_setup, run_key: str) -> None:
    _, _, ledger = ledger_setup

    with pytest.raises(ValueError, match="leading or trailing whitespace"):
        ledger.enqueue(run_key, SNAPSHOT)


@pytest.mark.parametrize("worker", [" leading", "trailing ", " both "])
def test_claim_rejects_workers_with_outer_whitespace(ledger_setup, worker: str) -> None:
    _, _, ledger = ledger_setup
    ledger.enqueue("outer-whitespace-worker", SNAPSHOT)

    with pytest.raises(ValueError, match="leading or trailing whitespace"):
        ledger.claim(worker, NOW, timedelta(seconds=10))


def test_run_key_and_worker_preserve_exact_unicode_identity(ledger_setup) -> None:
    _, _, ledger = ledger_setup
    run_key_nfc = "instrument:\u00e9"
    run_key_nfd = "instrument:e\u0301"
    worker_nfc = "worker:\u00e9"
    worker_nfd = "worker:e\u0301"

    first = ledger.enqueue(run_key_nfc, SNAPSHOT)
    claim = ledger.claim(worker_nfc, NOW, timedelta(seconds=10))
    assert claim is not None
    assert claim.run_key == run_key_nfc
    assert claim.worker == worker_nfc
    assert first.run_key != run_key_nfd

    assert (
        ledger.heartbeat(
            run_key_nfc,
            worker_nfd,
            claim.fencing_token,
            NOW + timedelta(seconds=1),
            timedelta(seconds=10),
        )
        is None
    )
    assert (
        ledger.heartbeat(
            run_key_nfc,
            worker_nfc,
            claim.fencing_token,
            NOW + timedelta(seconds=1),
            timedelta(seconds=10),
        )
        is not None
    )

    second = ledger.enqueue(run_key_nfd, SNAPSHOT)
    assert second.run_key == run_key_nfd
    current_nfc = ledger.get(run_key_nfc)
    assert current_nfc is not None
    assert current_nfc.run_key == first.run_key
    assert ledger.get(run_key_nfd) == second


def test_rejects_naive_non_utc_invalid_ttl_and_bad_transition_inputs(ledger_setup) -> None:
    _, _, ledger = ledger_setup
    ledger.enqueue("validation", SNAPSHOT)

    with pytest.raises(ValueError, match="timezone-aware"):
        ledger.claim("worker", NOW.replace(tzinfo=None), timedelta(seconds=1))
    with pytest.raises(ValueError, match="UTC"):
        ledger.claim(
            "worker",
            datetime(2026, 10, 26, 9, 30, tzinfo=timezone(timedelta(hours=8))),
            timedelta(seconds=1),
        )
    for invalid_ttl in (timedelta(0), timedelta(days=1, seconds=1)):
        with pytest.raises(ValueError, match="ttl"):
            ledger.claim("worker", NOW, invalid_ttl)
    with pytest.raises(ValueError, match="ttl"):
        ledger.claim("worker", NOW, 10)
    with pytest.raises(ValueError, match="non-empty"):
        ledger.claim(" ", NOW, timedelta(seconds=1))
    with pytest.raises(ValueError, match="worker must be at most"):
        ledger.claim("w" * 256, NOW, timedelta(seconds=1))
    with pytest.raises(ValueError, match="run_key must be at most"):
        ledger.get("k" * 256)
    with pytest.raises(ValueError, match="fencing_token"):
        ledger.heartbeat("validation", "worker", -1, NOW, timedelta(seconds=1))
    with pytest.raises(ValueError, match="fencing_token"):
        ledger.heartbeat("validation", "worker", True, NOW, timedelta(seconds=1))
    with pytest.raises(ValueError, match="error_text"):
        ledger.fail("validation", "worker", 1, NOW, "  ")
    with pytest.raises(ValueError, match="error_text"):
        ledger.fail("validation", "worker", 1, NOW, None)


def test_rejects_runtime_type_and_unicode_errors_before_inserting(ledger_setup) -> None:
    _, _, ledger = ledger_setup
    with pytest.raises(TypeError, match="synchronous SQLAlchemy Engine"):
        RunLedger(None)
    with pytest.raises(ValueError, match="JSON string"):
        ledger.enqueue("no-json", None)
    with pytest.raises(ValueError, match="UTF-8"):
        ledger.enqueue("bad-unicode", '{"value":"\ud800"}')
    with pytest.raises(ValueError, match="timezone-aware"):
        ledger.claim("worker", "2026-10-26T00:00:00Z", timedelta(seconds=1))
    assert ledger.get("no-json") is None
    assert ledger.get("bad-unicode") is None


def test_constructor_and_repository_do_not_create_tables_implicitly(tmp_path: Path) -> None:
    engine = _database_engine(tmp_path / "no-schema.sqlite")
    ledger = RunLedger(engine)
    try:
        with pytest.raises(OperationalError):
            ledger.get("absent")
    finally:
        engine.dispose()
