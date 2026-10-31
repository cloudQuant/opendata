"""Offline authorization and receipt-readback boundary tests for CSI 500."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, cast

import pytest

from alembic_source_gate.path_contract import DatabaseFileIdentity
from opendata.pipeline import ths_csi500_sample_composition as sample

if TYPE_CHECKING:
    from pathlib import Path

    from opendata.data.source_gate_quota_provisioning import Csi500SampleQuotaReceipt


def _make_authorized_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    expires_at: datetime | None = None,
    quota_identity_sha256: str | None = None,
) -> sample.SampleTarget:
    """Create a manifest-only target under pytest's private temporary root."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(sample, "SAMPLE_TEMP_ROOT", tmp_path)
    target = sample.create_sample_target()
    expiry = expires_at or datetime.now(timezone.utc) + timedelta(minutes=5)
    expiry_text = sample._format_aware_timestamp(expiry)
    state: dict[str, object] = {
        "calendar_date": (date.today() - timedelta(days=10)).isoformat(),
        "calendar_manifest_sha256": "a" * 64,
        "calendar_artifact_sha256": "b" * 64,
        "calendar_source_reference": "offline test fixture",
        "calendar_version": "offline-fixture-1",
        "calendar_reviewed_by": "offline-reviewer",
        "authorization_actor": "offline-operator",
        "source_rights_basis": "USER_DECLARED_UNVERIFIED",
        "grant_id": "csi500-sample-test-grant",
        "principal_scope": "local-research-service",
        "grant_expires_at_utc": expiry_text,
        "rights_evidence_sha256": "d" * 64,
        "quota_key": sample.SAMPLE_QUOTA_KEY,
        "quota_policy_revision": 1,
        "quota_policy_sha256": "f" * 64,
        "quota_binding_sha256": "e" * 64,
        "quota_target_identity_sha256": quota_identity_sha256
        or sample._sample_identity_sha256(target.identity),
    }
    target = sample.transition_sample_target(
        target,
        expected_phase="created",
        new_phase="migration_started",
    )
    target = sample.transition_sample_target(
        target,
        expected_phase="migration_started",
        new_phase="source_gate_initialized",
    )
    target = sample.transition_sample_target(
        target,
        expected_phase="source_gate_initialized",
        new_phase="authorization_started",
        updates={key: value for key, value in state.items() if not key.startswith("quota_")},
    )
    return sample.transition_sample_target(
        target,
        expected_phase="authorization_started",
        new_phase="authorized",
        updates={key: value for key, value in state.items() if key.startswith("quota_")},
    )


def _seed_gate_rows(target: sample.SampleTarget) -> None:
    """Seed the smallest local SQLite schema used by authorization readback."""
    state = target.state
    with sqlite3.connect(target.database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE od_source_grants (
                grant_id TEXT, source TEXT, canonical_model TEXT, operation TEXT,
                principal_scope TEXT, decision TEXT, revision INTEGER,
                rights_evidence_reference TEXT, rights_evidence_sha256 TEXT,
                expires_at_us INTEGER
            );
            CREATE TABLE od_source_grant_hosts (grant_id TEXT, host TEXT);
            CREATE TABLE od_source_grant_host_sets (
                grant_id TEXT, revision INTEGER, host_count INTEGER,
                sealed_by TEXT, reason_code TEXT
            );
            CREATE TABLE od_source_grant_events (
                grant_id TEXT, event_type TEXT, old_revision INTEGER,
                new_revision INTEGER, actor TEXT, reason_code TEXT
            );
            CREATE TABLE od_source_gate_source_bindings (
                source TEXT, quota_key TEXT, binding_sha256 TEXT,
                binding_reference TEXT, configured_by TEXT
            );
            CREATE TABLE od_source_gate_policies (
                quota_key TEXT, policy_revision INTEGER,
                rate_microtokens_per_second INTEGER, capacity_microtokens INTEGER,
                policy_sha256 TEXT, policy_reference TEXT, configured_by TEXT
            );
            CREATE TABLE od_source_gate_state (
                quota_key TEXT, tokens_micro INTEGER, policy_revision INTEGER,
                remainder INTEGER, last_refill_us INTEGER
            );
            CREATE TABLE od_source_attempt_reservations (
                attempt_no INTEGER, outcome TEXT, grant_id TEXT, run_key TEXT
            );
            CREATE TABLE od_source_attempt_scope_v2 (marker INTEGER);
            CREATE TABLE od_source_attempt_scope_v3 (marker INTEGER);
            CREATE TABLE od_source_one_shot_configs (marker INTEGER);
            CREATE TABLE od_source_one_shot_permits (marker INTEGER);
            """
        )
        expires_at_us = sample._epoch_microseconds(
            sample._parse_aware_timestamp(state["grant_expires_at_utc"])
        )
        connection.execute(
            "INSERT INTO od_source_grants VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                state["grant_id"],
                sample.SAMPLE_SOURCE,
                sample.SAMPLE_MODEL,
                "query",
                state["principal_scope"],
                "ALLOWED",
                1,
                sample.SAMPLE_EVIDENCE_REFERENCE,
                state["rights_evidence_sha256"],
                expires_at_us,
            ),
        )
        connection.execute(
            "INSERT INTO od_source_grant_hosts VALUES (?, ?)",
            (state["grant_id"], "fuyao.aicubes.cn"),
        )
        connection.execute(
            "INSERT INTO od_source_grant_host_sets VALUES (?, ?, ?, ?, ?)",
            (state["grant_id"], 1, 1, state["authorization_actor"], "operator-self-declaration"),
        )
        connection.execute(
            "INSERT INTO od_source_grant_events VALUES (?, ?, ?, ?, ?, ?)",
            (
                state["grant_id"],
                "HOSTS_SEALED",
                1,
                1,
                state["authorization_actor"],
                "operator-self-declaration",
            ),
        )
        connection.execute(
            "INSERT INTO od_source_gate_source_bindings VALUES (?, ?, ?, ?, ?)",
            (
                sample.SAMPLE_SOURCE,
                state["quota_key"],
                state["quota_binding_sha256"],
                sample.SAMPLE_EVIDENCE_REFERENCE,
                state["authorization_actor"],
            ),
        )
        connection.execute(
            "INSERT INTO od_source_gate_policies VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                state["quota_key"],
                state["quota_policy_revision"],
                sample.SAMPLE_QUOTA_RATE_MICROTOKENS_PER_SECOND,
                sample.SAMPLE_QUOTA_CAPACITY_MICROTOKENS,
                state["quota_policy_sha256"],
                sample.SAMPLE_EVIDENCE_REFERENCE,
                state["authorization_actor"],
            ),
        )
        connection.execute(
            "INSERT INTO od_source_gate_state VALUES (?, ?, ?, ?, ?)",
            (
                state["quota_key"],
                sample.SAMPLE_QUOTA_CAPACITY_MICROTOKENS,
                state["quota_policy_revision"],
                0,
                0,
            ),
        )


def _sample_quota_receipt(
    target: sample.SampleTarget,
    *,
    identity_sha256: str | None = None,
) -> Csi500SampleQuotaReceipt:
    """Build a receipt fixture with an optional mismatched database pin."""
    from opendata.data.source_gate_quota_provisioning import (
        Csi500SampleQuotaReceipt,
        Csi500SampleQuotaStatus,
    )

    return Csi500SampleQuotaReceipt(
        status=Csi500SampleQuotaStatus.PROVISIONED,
        grant_id=str(target.state["grant_id"]),
        source="ths",
        quota_key=sample.SAMPLE_QUOTA_KEY,
        policy_revision=1,
        binding_sha256="e" * 64,
        policy_sha256="f" * 64,
        rate_microtokens_per_second=sample.SAMPLE_QUOTA_RATE_MICROTOKENS_PER_SECOND,
        capacity_microtokens=sample.SAMPLE_QUOTA_CAPACITY_MICROTOKENS,
        target_identity_sha256=identity_sha256 or sample._sample_identity_sha256(target.identity),
    )


def _make_initialized_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> sample.SampleTarget:
    """Create a source-gate-initialized manifest without migrating a database."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(sample, "SAMPLE_TEMP_ROOT", tmp_path)
    target = sample.create_sample_target()
    target = sample.transition_sample_target(
        target,
        expected_phase="created",
        new_phase="migration_started",
    )
    return sample.transition_sample_target(
        target,
        expected_phase="migration_started",
        new_phase="source_gate_initialized",
    )


def _offline_calendar_evidence() -> sample.Csi500TradingDayEvidence:
    """Return synthetic historical evidence metadata for boundary tests."""
    return sample.Csi500TradingDayEvidence(
        business_date=date.today() - timedelta(days=10),
        calendar_version="offline-fixture-1",
        source_reference="offline test fixture",
        reviewed_by="offline-reviewer",
        manifest_sha256="a" * 64,
        artifact_sha256="b" * 64,
    )


def test_authorization_readback_accepts_matching_local_grant_quota_and_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A complete private readback validates the recorded grant and quota pins."""
    target = _make_authorized_target(tmp_path, monkeypatch)
    _seed_gate_rows(target)
    monkeypatch.setattr(
        sample,
        "_read_migration_state",
        lambda _target: sample.SAMPLE_EXPECTED_MIGRATION_STATE,
    )

    assert sample._validate_authorization_configuration(target) is None


@pytest.mark.parametrize(
    ("statement", "parameters"),
    [
        (
            "UPDATE od_source_grants SET principal_scope = ?",
            ("different-research-service",),
        ),
        (
            "UPDATE od_source_gate_source_bindings SET configured_by = ?",
            ("different-operator",),
        ),
        (
            "UPDATE od_source_gate_policies SET policy_sha256 = ?",
            ("0" * 64,),
        ),
        ("UPDATE od_source_gate_state SET tokens_micro = ?", (0,)),
    ],
)
def test_authorization_readback_rejects_scope_caller_or_quota_row_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    statement: str,
    parameters: tuple[object, ...],
) -> None:
    """Readback refuses a grant principal, operator, policy, or bucket drift."""
    target = _make_authorized_target(tmp_path, monkeypatch)
    _seed_gate_rows(target)
    with sqlite3.connect(target.database_path) as connection:
        connection.execute(statement, parameters)
    monkeypatch.setattr(
        sample,
        "_read_migration_state",
        lambda _target: sample.SAMPLE_EXPECTED_MIGRATION_STATE,
    )

    with pytest.raises(sample.Csi500SampleCompositionError) as error:
        sample._validate_authorization_configuration(target)

    assert error.value.code == "source_gate_configuration_mismatch"


def test_authorization_state_rejects_expired_grant_and_other_target_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An expired grant and a quota receipt for another file are both refused."""
    expired_target = _make_authorized_target(
        tmp_path / "expired",
        monkeypatch,
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    with pytest.raises(sample.Csi500SampleCompositionError) as expired_error:
        sample._validate_authorization_state(expired_target)
    assert expired_error.value.code == "source_grant_expired"

    wrong_identity_target = _make_authorized_target(
        tmp_path / "wrong-identity",
        monkeypatch,
        quota_identity_sha256="0" * 64,
    )
    with pytest.raises(sample.Csi500SampleCompositionError) as identity_error:
        sample._validate_authorization_state(wrong_identity_target)
    assert identity_error.value.code == "quota_target_identity_mismatch"


def test_quota_receipt_rejects_a_receipt_for_a_different_database_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid-looking quota receipt cannot be reused for another target file."""
    target = _make_authorized_target(tmp_path, monkeypatch)
    receipt = _sample_quota_receipt(target, identity_sha256="0" * 64)

    with pytest.raises(sample.Csi500SampleCompositionError) as error:
        sample._validate_sample_quota_receipt(
            receipt,
            grant_id=str(target.state["grant_id"]),
            principal_scope=str(target.state["principal_scope"]),
            expected_identity=target.identity,
        )

    assert error.value.code == "quota_receipt_target_identity_mismatch"


def test_grant_receipt_rejects_wrong_operator_and_expiration() -> None:
    """The committed grant receipt must echo the same caller and expiry."""
    from opendata.data.source_grant_provisioning import (
        SourceGrantProvisioningStatus,
        SourceGrantProvisionResult,
    )

    expiry = datetime.now(timezone.utc) + timedelta(minutes=5)
    expected_expiry_us = int(expiry.timestamp() * 1_000_000)
    wrong_actor = SourceGrantProvisionResult(
        status=SourceGrantProvisioningStatus.PROVISIONED,
        grant_id="csi500-sample-test-grant",
        revision=1,
        host="fuyao.aicubes.cn",
        not_before_us=expected_expiry_us - 300_000_000,
        expires_at_us=expected_expiry_us,
        attested_by="someone-else",
    )
    with pytest.raises(sample.Csi500SampleCompositionError) as actor_error:
        sample._validate_sample_grant_result(
            wrong_actor,
            grant_id="csi500-sample-test-grant",
            host="fuyao.aicubes.cn",
            expires_at=expiry,
            actor="offline-operator",
        )
    assert actor_error.value.code == "source_grant_receipt_invalid"

    wrong_expiry = SourceGrantProvisionResult(
        status=SourceGrantProvisioningStatus.PROVISIONED,
        grant_id="csi500-sample-test-grant",
        revision=1,
        host="fuyao.aicubes.cn",
        not_before_us=expected_expiry_us - 300_000_000,
        expires_at_us=expected_expiry_us + 2_000_000,
        attested_by="offline-operator",
    )
    with pytest.raises(sample.Csi500SampleCompositionError) as expiry_error:
        sample._validate_sample_grant_result(
            wrong_expiry,
            grant_id="csi500-sample-test-grant",
            host="fuyao.aicubes.cn",
            expires_at=expiry,
            actor="offline-operator",
        )
    assert expiry_error.value.code == "source_grant_expiry_mismatch"


def test_local_authorization_readback_failure_seals_target_without_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed post-write readback keeps the target sealed and avoids duplicate writes."""
    from opendata.data import source_gate_quota_provisioning as quota_module
    from opendata.data import source_grant_provisioning as grant_module

    monkeypatch.setattr(sample, "SAMPLE_TEMP_ROOT", tmp_path)
    target = sample.create_sample_target()
    target = sample.transition_sample_target(
        target,
        expected_phase="created",
        new_phase="migration_started",
    )
    target = sample.transition_sample_target(
        target,
        expected_phase="migration_started",
        new_phase="source_gate_initialized",
    )
    evidence = sample.Csi500TradingDayEvidence(
        business_date=date.today() - timedelta(days=10),
        calendar_version="offline-fixture-1",
        source_reference="offline test fixture",
        reviewed_by="offline-reviewer",
        manifest_sha256="a" * 64,
        artifact_sha256="b" * 64,
    )
    grant_writes: list[object] = []
    quota_writes: list[object] = []

    def provision_grant(_provisioner: object, request: object) -> object:
        grant_writes.append(request)
        return grant_module.SourceGrantProvisionResult(
            status=grant_module.SourceGrantProvisioningStatus.PROVISIONED,
            grant_id=request.grant_id,
            revision=request.revision,
            host=request.host,
            not_before_us=int(request.not_before.timestamp() * 1_000_000),
            expires_at_us=int(request.expires_at.timestamp() * 1_000_000),
            attested_by=request.attested_by,
        )

    def provision_quota(**values: object) -> object:
        quota_writes.append(values)
        return quota_module.Csi500SampleQuotaReceipt(
            status=quota_module.Csi500SampleQuotaStatus.PROVISIONED,
            grant_id=str(values["grant_id"]),
            source="ths",
            quota_key=sample.SAMPLE_QUOTA_KEY,
            policy_revision=1,
            binding_sha256="e" * 64,
            policy_sha256="f" * 64,
            rate_microtokens_per_second=sample.SAMPLE_QUOTA_RATE_MICROTOKENS_PER_SECOND,
            capacity_microtokens=sample.SAMPLE_QUOTA_CAPACITY_MICROTOKENS,
            target_identity_sha256=sample._sample_identity_sha256(target.identity),
        )

    monkeypatch.setattr(grant_module.SourceGrantProvisioner, "provision", provision_grant)
    monkeypatch.setattr(quota_module, "provision_csi500_sample_quota", provision_quota)
    monkeypatch.setattr(
        sample,
        "_read_migration_state",
        lambda _target: sample.SAMPLE_EXPECTED_MIGRATION_STATE,
    )
    monkeypatch.setattr(sample, "_require_fresh_sample_gate", lambda _target: None)

    def reject_readback(*_args: object, **_kwargs: object) -> None:
        raise sample.Csi500SampleCompositionError("source_gate_configuration_mismatch")

    monkeypatch.setattr(sample, "_validate_authorization_configuration", reject_readback)

    with (
        sample.lock_sample_target(target, operation="authorization"),
        pytest.raises(sample.Csi500SampleCompositionError) as error,
    ):
        sample.provision_sample_source_authorization(
            target,
            calendar_evidence=evidence,
            actor="offline-operator",
            principal_scope="local-research-service",
            confirm_user_declared_internal_use=True,
        )

    sealed = sample.load_sample_target(target.directory)
    assert error.value.code == "source_gate_configuration_mismatch"
    assert sealed.phase == "authorization_started"
    assert len(grant_writes) == len(quota_writes) == 1

    with (
        sample.lock_sample_target(sealed, operation="authorization"),
        pytest.raises(sample.Csi500SampleCompositionError) as retry_error,
    ):
        sample.provision_sample_source_authorization(
            sealed,
            calendar_evidence=evidence,
            actor="offline-operator",
            principal_scope="local-research-service",
            confirm_user_declared_internal_use=True,
        )
    assert retry_error.value.code == "target_not_source_gate_initialized"
    assert len(grant_writes) == len(quota_writes) == 1


def test_authorization_rejects_a_stale_database_identity_before_gate_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller-held identity pin cannot authorize a different SQLite inode."""
    monkeypatch.setattr(sample, "SAMPLE_TEMP_ROOT", tmp_path)
    target = sample.create_sample_target()
    target = sample.transition_sample_target(
        target,
        expected_phase="created",
        new_phase="migration_started",
    )
    target = sample.transition_sample_target(
        target,
        expected_phase="migration_started",
        new_phase="source_gate_initialized",
    )
    evidence = sample.Csi500TradingDayEvidence(
        business_date=date.today() - timedelta(days=10),
        calendar_version="offline-fixture-1",
        source_reference="offline test fixture",
        reviewed_by="offline-reviewer",
        manifest_sha256="a" * 64,
        artifact_sha256="b" * 64,
    )
    stale = sample.SampleTarget(
        target.directory,
        target.database_path,
        DatabaseFileIdentity(target.identity.device, target.identity.inode + 1),
        target.phase,
        target.state,
    )
    monkeypatch.setattr(
        sample,
        "_read_migration_state",
        lambda _target: pytest.fail("identity mismatch must stop before migration reads"),
    )

    with pytest.raises(sample.Csi500SampleCompositionError) as error:
        sample.provision_sample_source_authorization(
            stale,
            calendar_evidence=evidence,
            actor="offline-operator",
            principal_scope="local-research-service",
            confirm_user_declared_internal_use=True,
        )

    assert error.value.code == "target_not_source_gate_initialized"
    assert sample.load_sample_target(target.directory).phase == "source_gate_initialized"


@pytest.mark.parametrize(
    ("case", "expected_code"),
    [
        ("wrong-type", "quota_receipt_type_invalid"),
        ("wrong-grant", "quota_receipt_scope_mismatch"),
        ("not-committed", "quota_receipt_not_committed"),
        ("wrong-revision-type", "quota_receipt_invalid"),
        ("bad-policy-digest", "quota_receipt_invalid"),
    ],
)
def test_quota_receipt_scope_status_and_shape_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    expected_code: str,
) -> None:
    """Reject receipts that do not prove the expected scoped quota setup."""
    receipt = _sample_quota_receipt(_make_authorized_target(tmp_path, monkeypatch))
    if case == "wrong-type":
        candidate: object = object()
    elif case == "wrong-grant":
        candidate = replace(receipt, grant_id="another-grant")
    elif case == "not-committed":
        candidate = replace(receipt, status="PENDING")
    elif case == "wrong-revision-type":
        candidate = replace(receipt, policy_revision=True)
    else:
        candidate = replace(receipt, policy_sha256="not-a-sha256")

    with pytest.raises(sample.Csi500SampleCompositionError) as error:
        sample._validate_sample_quota_receipt(
            candidate,
            grant_id="csi500-sample-test-grant",
            principal_scope="local-research-service",
            expected_identity=DatabaseFileIdentity(1, 1),
        )

    assert error.value.code == expected_code


def test_authorization_state_rejects_incomplete_invalid_and_malformed_expiry_pins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing receipts, malformed digests, and unparseable expiry block use."""
    target = _make_authorized_target(tmp_path, monkeypatch)
    missing = dict(target.state)
    missing.pop("quota_binding_sha256")
    invalid_digest = dict(target.state)
    invalid_digest["rights_evidence_sha256"] = "d" * 63
    malformed_expiry = dict(target.state)
    malformed_expiry["grant_expires_at_utc"] = "not-a-timestamp"

    for state, expected_code in (
        (missing, "authorization_receipt_incomplete"),
        (invalid_digest, "authorization_receipt_invalid"),
        (malformed_expiry, "timestamp_invalid"),
    ):
        with pytest.raises(sample.Csi500SampleCompositionError) as error:
            sample._validate_authorization_state(target, state=state)
        assert error.value.code == expected_code


def test_authorization_preconditions_reject_wrong_calendar_type_before_target_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An invalid evidence object is rejected before even loading a target."""
    target = _make_initialized_target(tmp_path, monkeypatch)
    monkeypatch.setattr(
        sample,
        "load_sample_target",
        lambda _directory: pytest.fail("invalid calendar evidence must fail before target reads"),
    )

    with pytest.raises(sample.Csi500SampleCompositionError) as error:
        sample.provision_sample_source_authorization(
            target,
            calendar_evidence=cast("sample.Csi500TradingDayEvidence", object()),
            actor="offline-operator",
            principal_scope="local-research-service",
            confirm_user_declared_internal_use=True,
        )

    assert error.value.code == "calendar_evidence_invalid"


def test_authorization_migration_mismatch_stops_before_any_grant_or_quota_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale migration head blocks authorization before write-side imports."""
    target = _make_initialized_target(tmp_path, monkeypatch)
    evidence = _offline_calendar_evidence()
    monkeypatch.setattr(
        sample,
        "_read_migration_state",
        lambda _target: sample.SampleMigrationState(
            warehouse_schema_status="NOT_APPLIED",
            source_gate_head="sg0003_stale_head",
        ),
    )
    monkeypatch.setattr(
        sample,
        "_require_fresh_sample_gate",
        lambda _target: pytest.fail("a mismatched migration head must stop first"),
    )

    with (
        sample.lock_sample_target(target, operation="authorization"),
        pytest.raises(sample.Csi500SampleCompositionError) as error,
    ):
        sample.provision_sample_source_authorization(
            target,
            calendar_evidence=evidence,
            actor="offline-operator",
            principal_scope="local-research-service",
            confirm_user_declared_internal_use=True,
        )

    assert error.value.code == "migration_state_mismatch"
    assert sample.load_sample_target(target.directory).phase == "source_gate_initialized"


def test_invalid_committed_grant_receipt_leaves_authorization_sealed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A grant receipt for another operator is re-raised and never reaches quota."""
    from opendata.data import source_gate_quota_provisioning as quota_module
    from opendata.data import source_grant_provisioning as grant_module

    target = _make_initialized_target(tmp_path, monkeypatch)
    grant_requests: list[object] = []
    quota_calls: list[object] = []

    def provision_wrong_grant(_provisioner: object, request: object) -> object:
        grant_requests.append(request)
        return grant_module.SourceGrantProvisionResult(
            status=grant_module.SourceGrantProvisioningStatus.PROVISIONED,
            grant_id=request.grant_id,
            revision=1,
            host=request.host,
            not_before_us=int(request.not_before.timestamp() * 1_000_000),
            expires_at_us=int(request.expires_at.timestamp() * 1_000_000),
            attested_by="different-operator",
        )

    def fail_if_quota_called(**values: object) -> None:
        quota_calls.append(values)
        pytest.fail("quota must not be configured for a mismatched grant receipt")

    monkeypatch.setattr(
        grant_module.SourceGrantProvisioner,
        "provision",
        provision_wrong_grant,
    )
    monkeypatch.setattr(
        quota_module,
        "provision_csi500_sample_quota",
        fail_if_quota_called,
    )
    monkeypatch.setattr(
        sample,
        "_read_migration_state",
        lambda _target: sample.SAMPLE_EXPECTED_MIGRATION_STATE,
    )
    monkeypatch.setattr(sample, "_require_fresh_sample_gate", lambda _target: None)

    with (
        sample.lock_sample_target(target, operation="authorization"),
        pytest.raises(sample.Csi500SampleCompositionError) as error,
    ):
        sample.provision_sample_source_authorization(
            target,
            calendar_evidence=_offline_calendar_evidence(),
            actor="offline-operator",
            principal_scope="local-research-service",
            confirm_user_declared_internal_use=True,
        )

    sealed = sample.load_sample_target(target.directory)
    assert error.value.code == "source_grant_receipt_invalid"
    assert sealed.phase == "authorization_started"
    assert len(grant_requests) == 1
    assert quota_calls == []


def test_engine_initialization_failure_is_uncertain_and_seals_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failure after the intent transition leaves a conservative no-retry state."""
    target = _make_initialized_target(tmp_path, monkeypatch)
    monkeypatch.setattr(
        sample,
        "_read_migration_state",
        lambda _target: sample.SAMPLE_EXPECTED_MIGRATION_STATE,
    )
    monkeypatch.setattr(sample, "_require_fresh_sample_gate", lambda _target: None)

    def fail_engine(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("synthetic local engine setup failure")

    monkeypatch.setattr(sample, "create_engine", fail_engine)
    with (
        sample.lock_sample_target(target, operation="authorization"),
        pytest.raises(sample.Csi500SampleCompositionError) as error,
    ):
        sample.provision_sample_source_authorization(
            target,
            calendar_evidence=_offline_calendar_evidence(),
            actor="offline-operator",
            principal_scope="local-research-service",
            confirm_user_declared_internal_use=True,
        )

    assert error.value.code == "authorization_outcome_uncertain"
    assert sample.load_sample_target(target.directory).phase == "authorization_started"


def test_authorization_readback_rejects_stale_target_identity_and_migration_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The post-write readback is pinned to both the exact inode and migration head."""
    target = _make_authorized_target(tmp_path, monkeypatch)
    _seed_gate_rows(target)
    stale_target = sample.SampleTarget(
        target.directory,
        target.database_path,
        DatabaseFileIdentity(target.identity.device, target.identity.inode + 1),
        target.phase,
        target.state,
    )
    with pytest.raises(sample.Csi500SampleCompositionError) as identity_error:
        sample._validate_authorization_configuration(stale_target)
    assert identity_error.value.code == "target_identity_changed"

    monkeypatch.setattr(
        sample,
        "_read_migration_state",
        lambda _target: sample.SampleMigrationState(
            warehouse_schema_status="NOT_APPLIED",
            source_gate_head="sg0003_stale_head",
        ),
    )
    with pytest.raises(sample.Csi500SampleCompositionError) as migration_error:
        sample._validate_authorization_configuration(target)
    assert migration_error.value.code == "migration_state_mismatch"


def test_authorization_readback_rejects_absent_and_residual_gate_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An absent schema or residual V3 evidence cannot look like a fresh grant."""
    target = _make_authorized_target(tmp_path, monkeypatch)
    with pytest.raises(sample.Csi500SampleCompositionError) as missing_error:
        sample._validate_source_gate_rows(target, allow_one_attempt=False)
    assert missing_error.value.code == "source_gate_readback_unavailable"

    _seed_gate_rows(target)
    with sqlite3.connect(target.database_path) as connection:
        connection.execute("INSERT INTO od_source_attempt_scope_v3 VALUES (1)")
    with pytest.raises(sample.Csi500SampleCompositionError) as residual_error:
        sample._validate_source_gate_rows(target, allow_one_attempt=False)
    assert residual_error.value.code == "source_gate_target_not_fresh"


def test_fresh_gate_rejects_migration_mismatch_and_missing_clock_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Authorization requires the pinned migration and a readable gate clock."""
    target = _make_initialized_target(tmp_path, monkeypatch)
    monkeypatch.setattr(
        sample,
        "_read_migration_state",
        lambda _target: sample.SampleMigrationState(
            warehouse_schema_status="NOT_APPLIED",
            source_gate_head="sg0003_stale_head",
        ),
    )
    with pytest.raises(sample.Csi500SampleCompositionError) as migration_error:
        sample._require_fresh_sample_gate(target)
    assert migration_error.value.code == "migration_state_mismatch"

    monkeypatch.setattr(
        sample,
        "_read_migration_state",
        lambda _target: sample.SAMPLE_EXPECTED_MIGRATION_STATE,
    )
    with pytest.raises(sample.Csi500SampleCompositionError) as schema_error:
        sample._require_fresh_sample_gate(target)
    assert schema_error.value.code == "source_gate_freshness_unavailable"
