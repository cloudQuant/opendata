"""Offline boundary tests for source-grant request and gate failures."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa

from opendata.data.source_attempt_gate import (
    SourceAttemptGate,
    SourceAttemptGateUnavailable,
    SourceAttemptInputError,
)
from opendata.data.source_grant_provisioning import (
    SourceGrantProvisioner,
    SourceGrantProvisioningUnavailableError,
    SourceGrantProvisionRequest,
)

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

    from sqlalchemy.engine import Connection, Engine


_EPOCH_UTC = datetime(1970, 1, 1, tzinfo=timezone.utc)


class _NoOffset(tzinfo):
    """Represent a malformed timezone with no usable UTC offset."""

    def utcoffset(self, _value: datetime | None) -> timedelta | None:
        return None

    def dst(self, _value: datetime | None) -> timedelta | None:
        return None

    def tzname(self, _value: datetime | None) -> str:
        return "synthetic-no-offset"


class _FailingOffset(tzinfo):
    """Represent a timezone implementation that fails while resolving its offset."""

    def utcoffset(self, _value: datetime | None) -> timedelta:
        raise RuntimeError("synthetic timezone detail")

    def dst(self, _value: datetime | None) -> timedelta:
        return timedelta(0)

    def tzname(self, _value: datetime | None) -> str:
        return "synthetic-failing-offset"


class _ConnectionMetadataUnavailable:
    """Represent a connection wrapper that cannot expose its DBAPI handle."""

    @property
    def connection(self) -> object:
        raise RuntimeError("synthetic connection metadata detail")


def _request(**overrides: object) -> SourceGrantProvisionRequest:
    """Build a synthetic, narrow grant declaration with fixed safe metadata."""
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    values: dict[str, object] = {
        "grant_id": "synthetic-boundary-grant",
        "revision": 1,
        "source": "synthetic-source",
        "canonical_model": "synthetic-model",
        "operation": "query",
        "principal_scope": "synthetic-principal",
        "host": "api.example.test",
        "not_before": now,
        "expires_at": now + timedelta(hours=1),
        "rights_evidence_reference": "synthetic-test-only:operator-declaration",
        "rights_evidence_sha256": "a" * 64,
        "operator_confirms_authorized_use": True,
        "operator_attestation_note": "Synthetic declaration for an offline boundary test.",
        "attested_by": "synthetic-test-operator",
        "conditions": (),
    }
    values.update(overrides)
    return SourceGrantProvisionRequest(**values)  # type: ignore[arg-type]


@pytest.fixture
def file_backed_gate(tmp_path: Path) -> Generator[tuple[Engine, SourceAttemptGate], None, None]:
    """Create a file-backed gate object without opening a connection or reading data."""
    database_path = tmp_path / "source-grant-boundaries.sqlite"
    database_path.touch()
    engine = sa.create_engine(f"sqlite:///{database_path}")
    try:
        yield engine, SourceAttemptGate(engine)
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("overrides", "expected_message"),
    [
        ({"grant_id": 7}, "grant_id must be a string"),
        ({"host": 7}, "host must be a string"),
        ({"rights_evidence_reference": 7}, "evidence reference must be text"),
        (
            {"rights_evidence_reference": " evidence with boundary whitespace "},
            "evidence reference is invalid or contains credential-like material",
        ),
        ({"not_before": "2026-01-01T00:00:00Z"}, "not_before must be a timezone-aware datetime"),
        (
            {"not_before": datetime(2026, 1, 1, tzinfo=_NoOffset())},
            "not_before must be a timezone-aware datetime",
        ),
        (
            {"not_before": datetime(2026, 1, 1, tzinfo=_FailingOffset())},
            "not_before is outside the supported time range",
        ),
        (
            {"not_before": datetime(1969, 12, 31, 23, 59, 59, tzinfo=timezone.utc)},
            "not_before is outside the supported time range",
        ),
    ],
    ids=[
        "identifier-type",
        "host-type",
        "evidence-type",
        "evidence-whitespace",
        "timestamp-type",
        "timestamp-without-offset",
        "timezone-resolution-failure",
        "timestamp-before-supported-epoch",
    ],
)
def test_invalid_request_boundaries_fail_with_fixed_messages(
    overrides: dict[str, object],
    expected_message: str,
) -> None:
    """Malformed types and timestamps fail closed without echoing caller values."""
    with pytest.raises(SourceAttemptInputError) as error:
        _request(**overrides)

    assert str(error.value) == expected_message


def test_provisioner_and_provision_reject_untrusted_object_types(
    file_backed_gate: tuple[Engine, SourceAttemptGate],
) -> None:
    """The storage boundary requires the concrete gate and request contracts."""
    _engine, gate = file_backed_gate

    with pytest.raises(
        SourceAttemptInputError,
        match="^gate must be an explicit SourceAttemptGate$",
    ):
        SourceGrantProvisioner(object())  # type: ignore[arg-type]

    provisioner = SourceGrantProvisioner(gate)
    with pytest.raises(
        SourceAttemptInputError,
        match="^request must be a SourceGrantProvisionRequest$",
    ):
        provisioner.provision(object())  # type: ignore[arg-type]


def test_gate_open_failure_is_translated_without_exposing_storage_details(
    file_backed_gate: tuple[Engine, SourceAttemptGate],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unavailable writer is reported as a fixed safe error before any insert."""
    _engine, gate = file_backed_gate

    def fail_to_open() -> Connection:
        raise SourceAttemptGateUnavailable("synthetic database path detail")

    monkeypatch.setattr(gate, "_open_write_connection", fail_to_open)

    with pytest.raises(SourceGrantProvisioningUnavailableError) as error:
        SourceGrantProvisioner(gate).provision(_request())

    assert str(error.value) == "source gate transaction is unavailable"
    assert "synthetic database path detail" not in str(error.value)


@pytest.mark.parametrize(
    "connection",
    [
        SimpleNamespace(connection=SimpleNamespace(driver_connection=object())),
        _ConnectionMetadataUnavailable(),
    ],
    ids=["non-sqlite-driver", "driver-handle-unavailable"],
)
def test_schema_validation_rejects_connections_outside_the_pinned_sqlite_boundary(
    file_backed_gate: tuple[Engine, SourceAttemptGate],
    connection: object,
) -> None:
    """Schema checks fail closed when SQLAlchemy cannot provide native SQLite state."""
    _engine, gate = file_backed_gate

    with pytest.raises(SourceGrantProvisioningUnavailableError) as error:
        SourceGrantProvisioner(gate)._require_valid_gate_schema(connection)  # type: ignore[arg-type]

    assert str(error.value) == "source gate schema is unavailable"


def test_expired_request_is_rejected_against_the_gate_clock_before_lookup_or_write(
    file_backed_gate: tuple[Engine, SourceAttemptGate],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A declaration that expired before the trusted gate time cannot be provisioned."""
    _engine, gate = file_backed_gate
    expired_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
    request = _request(
        not_before=expired_at - timedelta(hours=1),
        expires_at=expired_at,
    )
    execute_calls: list[tuple[object, ...]] = []

    def fail_if_queried(*args: object) -> None:
        execute_calls.append(args)
        raise AssertionError("expired request must be rejected before any gate lookup")

    connection = SimpleNamespace(execute=fail_if_queried, close=lambda: None)

    def skip_schema_check(
        _provisioner: SourceGrantProvisioner,
        _connection: Connection,
    ) -> None:
        return None

    def report_gate_time_after_expiry(_connection: Connection) -> int:
        delta = expired_at - _EPOCH_UTC
        return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + 1

    monkeypatch.setattr(gate, "_open_write_connection", lambda: connection)
    monkeypatch.setattr(gate, "_advance_clock", report_gate_time_after_expiry)
    monkeypatch.setattr(SourceGrantProvisioner, "_require_valid_gate_schema", skip_schema_check)

    with pytest.raises(
        SourceAttemptInputError,
        match="^expires_at must be later than the source gate clock$",
    ):
        SourceGrantProvisioner(gate).provision(request)

    assert execute_calls == []
