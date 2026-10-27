"""Spawned-process contention tests for the file-backed SQLite attempt gate."""

from __future__ import annotations

import hashlib
import importlib.util
import logging
import multiprocessing
import socket
from contextlib import suppress
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Protocol
from unittest.mock import patch

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.pool import NullPool

from opendata.data.source_attempt_gate import (
    SourceAttemptGate,
    TransportAttempt,
)

if TYPE_CHECKING:
    from multiprocessing.connection import Connection as ProcessConnection
    from types import ModuleType

    from sqlalchemy.engine import Engine


class _BarrierLike(Protocol):
    """Typing contract for a process-safe two-party rendezvous."""

    def wait(self, timeout: float | None = None) -> object:
        """Wait until all parties arrive or the timeout expires."""


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    REPOSITORY_ROOT / "alembic_data" / "versions" / "20261027-0015_source_attempt_gate.py"
)
MIGRATION_NAME = "source_attempt_gate_migration_0015_spawn_test"
DATABASE_NAME = "multiprocess-source-attempt-gate.sqlite"
LOGGER = logging.getLogger(__name__)
SOURCE = "sina"
MODEL = "financial-statement"
PRINCIPAL_SCOPE = "opendata-worker"
HOST = "api.sina.example"
QUOTA_KEY = "sina-quota-process-test"
MICROTOKENS_PER_TOKEN = 1_000_000
MAX_TIMESTAMP_US = 253_402_300_799_999_999


def _load_migration() -> ModuleType:
    """Load only revision 0015 without evaluating Alembic's project environment."""
    spec = importlib.util.spec_from_file_location(MIGRATION_NAME, MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _new_engine(database_path: Path) -> Engine:
    """Open the test-owned SQLite file with one independent DBAPI connection per checkout."""
    return sa.create_engine(
        f"sqlite:///{database_path}",
        connect_args={"timeout": 15},
        poolclass=NullPool,
    )


def _apply_migration(engine: Engine) -> None:
    """Apply only the gate schema to the explicit temporary SQLite connection."""
    migration = _load_migration()
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()


def _database_future_us(engine: Engine) -> int:
    """Read this disposable SQLite file's clock and place the bucket high-water ahead."""
    with engine.connect() as connection:
        now_us = connection.exec_driver_sql(
            "SELECT CAST(ROUND((julianday('now') - 2440587.5) * 86400000000.0) AS INTEGER)"
        ).scalar_one()
    assert type(now_us) is int
    future_us = now_us + 86_400_000_000
    assert future_us < MAX_TIMESTAMP_US
    return future_us


def _seed_one_token(engine: Engine, highwater_us: int) -> None:
    """Create one valid synthetic grant and exactly one full shared quota token."""
    digest = hashlib.sha256(b"multiprocess-offline-test-evidence").hexdigest()
    with engine.begin() as connection:
        connection.execute(
            sa.text("UPDATE od_source_gate_clock SET highwater_us = :now_us WHERE clock_id = 1"),
            {"now_us": highwater_us},
        )
        connection.execute(
            sa.text(
                "INSERT INTO od_source_gate_policies (quota_key, policy_revision, "
                "rate_microtokens_per_second, capacity_microtokens, policy_reference, "
                "policy_sha256, configured_by, configured_at_us) VALUES "
                "(:quota_key, 1, 1, :capacity, 'synthetic-policy', :digest, 'offline-test', 1)"
            ),
            {"quota_key": QUOTA_KEY, "capacity": MICROTOKENS_PER_TOKEN, "digest": digest},
        )
        connection.execute(
            sa.text(
                "INSERT INTO od_source_gate_source_bindings (source, quota_key, "
                "binding_reference, binding_sha256, configured_by, configured_at_us) VALUES "
                "(:source, :quota_key, 'synthetic-binding', :digest, 'offline-test', 1)"
            ),
            {"source": SOURCE, "quota_key": QUOTA_KEY, "digest": digest},
        )
        connection.execute(
            sa.text(
                "INSERT INTO od_source_gate_state (quota_key, tokens_micro, remainder, "
                "last_refill_us, policy_revision) VALUES (:quota_key, :tokens, 0, :now_us, 1)"
            ),
            {
                "quota_key": QUOTA_KEY,
                "tokens": MICROTOKENS_PER_TOKEN,
                "now_us": highwater_us,
            },
        )
        connection.execute(
            sa.text(
                "INSERT INTO od_source_grants (grant_id, source, canonical_model, operation, "
                "principal_scope, decision, revision, not_before_us, expires_at_us, "
                "conditions_json, rights_evidence_reference, rights_evidence_sha256, "
                "operator_attestation_note, attested_by, attested_at_us, revoked_at_us, "
                "created_at_us, updated_at_us) VALUES ('grant-process', :source, :model, 'query', "
                ":principal, 'ALLOWED', 1, 0, :expires, '[]', 'synthetic-rights-ref', :digest, "
                "'Synthetic declaration only; no external source is called.', 'offline-test', "
                "1, NULL, 1, 1)"
            ),
            {
                "source": SOURCE,
                "model": MODEL,
                "principal": PRINCIPAL_SCOPE,
                "expires": MAX_TIMESTAMP_US,
                "digest": digest,
            },
        )
        connection.execute(
            sa.text(
                "INSERT INTO od_source_grant_hosts (grant_id, host) VALUES ('grant-process', :host)"
            ),
            {"host": HOST},
        )
        connection.execute(
            sa.text(
                "INSERT INTO od_source_grant_host_sets "
                "(grant_id, revision, host_count, sealed_at_us, sealed_by, reason_code) "
                "VALUES ('grant-process', 1, 1, 1, 'offline-test', 'rights-reviewed')"
            )
        )


def _network_is_forbidden(*_args: object, **_kwargs: object) -> None:
    """Fail if a child process attempts to create a network connection."""
    raise AssertionError("network access is forbidden in this SQLite-only test")


def _reserve_in_child(
    database_path: str,
    barrier: _BarrierLike,
    attempt_id: str,
    request_id: str,
    output: ProcessConnection,
) -> None:
    """Compete for one reservation in a spawned process with network calls blocked."""
    engine: Engine | None = None
    try:
        with (
            patch.object(socket.socket, "connect", side_effect=_network_is_forbidden),
            patch.object(socket.socket, "connect_ex", side_effect=_network_is_forbidden),
            patch("socket.create_connection", side_effect=_network_is_forbidden),
        ):
            engine = _new_engine(Path(database_path))
            gate = SourceAttemptGate(engine)
            barrier.wait(timeout=10)
            attempt = TransportAttempt(
                attempt_id=attempt_id,
                request_id=request_id,
                attempt_no=1,
                grant_id="grant-process",
                expected_revision=1,
                source=SOURCE,
                canonical_model=MODEL,
                operation="query",
                principal_scope=PRINCIPAL_SCOPE,
                host=HOST,
                run_key="offline-run",
            )
            result = gate.reserve_attempt(attempt)
            output.send(("ok", result.status.value, result.denial_code))
    except BaseException as exc:
        with suppress(BrokenPipeError, EOFError, OSError):
            output.send(("error", type(exc).__name__, str(exc)[:300]))
    finally:
        if engine is not None:
            engine.dispose()
        output.close()


def _cleanup_spawned_workers(
    processes: list[multiprocessing.Process],
    receivers: list[ProcessConnection],
    senders: list[ProcessConnection],
) -> None:
    """Close every pipe and reap each started child, escalating if it stalls."""
    for endpoint in (*receivers, *senders):
        with suppress(OSError, ValueError):
            endpoint.close()

    failures: list[str] = []
    for process in processes:
        if process.pid is None:
            continue
        try:
            alive = process.is_alive()
        except (AssertionError, OSError, ValueError) as exc:
            alive = True
            message = f"could not inspect spawned gate worker pid={process.pid}: {exc}"
            LOGGER.exception(message)
            failures.append(message)

        if alive:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            except (OSError, ValueError) as exc:
                message = f"could not terminate spawned gate worker pid={process.pid}: {exc}"
                LOGGER.exception(message)
                failures.append(message)

        try:
            process.join(timeout=5)
        except (AssertionError, OSError, ValueError) as exc:
            message = f"could not join spawned gate worker pid={process.pid}: {exc}"
            LOGGER.exception(message)
            failures.append(message)

        try:
            alive = process.is_alive()
        except (AssertionError, OSError, ValueError) as exc:
            alive = True
            message = f"could not inspect spawned gate worker pid={process.pid}: {exc}"
            LOGGER.exception(message)
            failures.append(message)

        if alive:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            except (OSError, ValueError) as exc:
                message = f"could not kill spawned gate worker pid={process.pid}: {exc}"
                LOGGER.exception(message)
                failures.append(message)
            try:
                process.join(timeout=5)
            except (AssertionError, OSError, ValueError) as exc:
                message = f"could not join killed gate worker pid={process.pid}: {exc}"
                LOGGER.exception(message)
                failures.append(message)

        try:
            if process.is_alive():
                message = f"spawned gate worker pid={process.pid} remained alive after kill/join"
                LOGGER.error(message)
                failures.append(message)
        except (AssertionError, OSError, ValueError) as exc:
            message = f"could not verify spawned gate worker pid={process.pid} exit: {exc}"
            LOGGER.exception(message)
            failures.append(message)

    if failures:
        raise RuntimeError("; ".join(failures))


def test_spawned_processes_serialize_the_last_token_without_network(
    tmp_path: Path,
) -> None:
    """Two independent spawned processes cannot both spend the same SQLite token."""
    database_path = tmp_path / DATABASE_NAME
    parent_engine = _new_engine(database_path)
    try:
        _apply_migration(parent_engine)
        highwater_us = _database_future_us(parent_engine)
        _seed_one_token(parent_engine, highwater_us)

        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(2)
        processes: list[multiprocessing.Process] = []
        receivers: list[ProcessConnection] = []
        senders: list[ProcessConnection] = []
        results: list[tuple[str, str, str | None]] = []
        try:
            for suffix in ("a", "b"):
                receiver, sender = context.Pipe(duplex=False)
                receivers.append(receiver)
                senders.append(sender)
                process = context.Process(
                    target=_reserve_in_child,
                    args=(
                        str(database_path),
                        barrier,
                        f"attempt-{suffix}",
                        f"request-{suffix}",
                        sender,
                    ),
                )
                processes.append(process)
                process.start()
                sender.close()

            result_deadline = monotonic() + 30
            for receiver in receivers:
                remaining = result_deadline - monotonic()
                if remaining <= 0 or not receiver.poll(remaining):
                    raise TimeoutError("spawned gate worker did not return before timeout")
                results.append(receiver.recv())

            for process in processes:
                process.join(timeout=10)
            assert all(not process.is_alive() for process in processes)
            assert all(process.exitcode == 0 for process in processes)
            assert len({process.pid for process in processes}) == 2
        finally:
            _cleanup_spawned_workers(processes, receivers, senders)

        assert all(result[0] == "ok" for result in results), results
        outcomes = sorted(result[1] for result in results)
        assert outcomes == ["DENIED", "RESERVED_NEW"]
        denied = next(result for result in results if result[1] == "DENIED")
        assert denied[2] == "source_rate_limited"

        with parent_engine.connect() as connection:
            attempts = (
                connection.execute(
                    sa.text(
                        "SELECT outcome, denial_code FROM od_source_attempt_reservations "
                        "ORDER BY attempt_id"
                    )
                )
                .mappings()
                .all()
            )
            bucket = (
                connection.execute(
                    sa.text(
                        "SELECT tokens_micro, remainder FROM od_source_gate_state "
                        "WHERE quota_key = :quota_key"
                    ),
                    {"quota_key": QUOTA_KEY},
                )
                .mappings()
                .one()
            )

        assert sorted(row["outcome"] for row in attempts) == ["DENIED", "RESERVED"]
        assert sum(row["denial_code"] == "source_rate_limited" for row in attempts) == 1
        assert (bucket["tokens_micro"], bucket["remainder"]) == (0, 0)
    finally:
        parent_engine.dispose()
