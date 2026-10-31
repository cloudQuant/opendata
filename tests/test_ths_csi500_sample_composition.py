"""Offline boundary checks for the isolated CSI 500 sample composition."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from opendata.pipeline import ths_csi500_sample_composition as sample
from opendata.pipeline.ths_index_daily_publication import (
    build_csi500_v4_sample_payload,
    build_csi500_v4_sample_spec,
)
from scripts.ops import run_csi500_one_shot_sample as sample_cli


@pytest.fixture
def isolated_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sample.SampleTarget:
    """Create only a private zero-byte SQLite target below pytest's temp root."""
    monkeypatch.setattr(sample, "SAMPLE_TEMP_ROOT", tmp_path)
    return sample.create_sample_target()


def test_target_manifest_id_must_match_directory_uuid(
    isolated_target: sample.SampleTarget,
) -> None:
    """A valid-shaped manifest ID cannot identify a different target directory."""
    manifest_path = isolated_target.directory / sample.SAMPLE_MANIFEST_FILENAME
    tampered_state = dict(isolated_target.state)
    original_id = str(tampered_state["target_id"])
    replacement_prefix = "0" if original_id[0] != "0" else "1"
    tampered_state["target_id"] = replacement_prefix + original_id[1:]
    manifest_path.write_text(
        json.dumps(tampered_state, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    with pytest.raises(sample.Csi500SampleCompositionError) as error:
        sample.load_sample_target(isolated_target.directory)

    assert error.value.code == "target_manifest_identity_mismatch"
    assert isolated_target.database_path.is_file()


def _write_calendar_evidence(directory: Path, business_date: date) -> Path:
    """Write a minimal frozen evidence pair for parser-only tests."""
    artifact = directory / "sse-calendar.txt"
    artifact.write_text(f"SSE session: {business_date.isoformat()}\n", encoding="utf-8")
    artifact_hash = hashlib.sha256(artifact.read_bytes()).hexdigest()
    manifest = directory / "calendar-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "exchange": "SSE",
                "calendar_version": "fixture-1",
                "source_reference": "offline fixture; not an authority proof",
                "retrieved_at_utc": "2026-10-01T00:00:00Z",
                "reviewed_by": "offline-reviewer",
                "artifact_path": str(artifact),
                "artifact_sha256": artifact_hash,
                "open_sessions": [business_date.isoformat()],
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    return manifest


def _patch_local_authorization_writers(
    monkeypatch: pytest.MonkeyPatch,
    *,
    target: sample.SampleTarget,
    quota_error: Exception | None = None,
) -> tuple[list[object], list[object], list[str]]:
    """Stub only grant/quota writes while preserving their request contracts."""
    from opendata.data import source_gate_quota_provisioning as quota_module
    from opendata.data import source_grant_provisioning as grant_module

    grant_requests: list[object] = []
    quota_requests: list[object] = []
    readback_calls: list[str] = []

    def provision_grant(_provisioner: object, request: object) -> object:
        grant_requests.append(request)
        return grant_module.SourceGrantProvisionResult(
            status=grant_module.SourceGrantProvisioningStatus.PROVISIONED,
            grant_id=request.grant_id,
            revision=request.revision,
            host=request.host,
            not_before_us=int(request.not_before.timestamp() * 1_000_000),
            expires_at_us=int(request.expires_at.timestamp() * 1_000_000),
            attested_by=request.attested_by,
        )

    def provision_quota(**kwargs: object) -> object:
        quota_requests.append(kwargs)
        if quota_error is not None:
            raise quota_error
        return quota_module.Csi500SampleQuotaReceipt(
            status=quota_module.Csi500SampleQuotaStatus.PROVISIONED,
            grant_id=str(kwargs["grant_id"]),
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
    monkeypatch.setattr(
        sample,
        "_validate_authorization_configuration",
        lambda _target, **_kwargs: readback_calls.append("rows"),
    )
    monkeypatch.setattr(
        sample,
        "_inspect_sample_gate_configuration",
        lambda _target, **_kwargs: readback_calls.append("gate"),
    )
    return grant_requests, quota_requests, readback_calls


def test_migration_environment_is_allowlisted_and_pins_private_home(
    isolated_target: sample.SampleTarget,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin Python's default text I/O independently of the host locale."""
    target = sample.transition_sample_target(
        isolated_target,
        expected_phase="created",
        new_phase="migration_started",
    )
    monkeypatch.setenv("PYTHONUTF8", "0")
    monkeypatch.setenv("DATABASE_URL", "mysql://must-not-leak/example")
    monkeypatch.setenv("HOME", "/host/home/must-not-leak")

    environment = sample_cli._migration_environment(target)

    assert environment == {
        "PATH": os.defpath,
        "LANG": "C.UTF-8",
        "PYTHONUTF8": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(sample_cli.PROJECT_ROOT),
        "HOME": str(target.directory),
    }


def test_migration_environment_rejects_unpinned_target_path(
    isolated_target: sample.SampleTarget,
    tmp_path: Path,
) -> None:
    """Do not accept an arbitrary caller path as the migration child's HOME."""
    target = sample.transition_sample_target(
        isolated_target,
        expected_phase="created",
        new_phase="migration_started",
    )
    external_directory = tmp_path / "external-home"
    external_directory.mkdir(mode=0o700)
    external_target = sample.SampleTarget(
        directory=external_directory,
        database_path=external_directory / sample.SAMPLE_DATABASE_FILENAME,
        identity=target.identity,
        phase="migration_started",
        state=target.state,
    )

    with pytest.raises(sample.Csi500SampleCompositionError) as error:
        sample_cli._migration_environment(external_target)

    assert error.value.code == "target_directory_identity_invalid"


def test_uncertain_source_gate_migration_seals_target_against_retry(
    isolated_target: sample.SampleTarget,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unconfirmed migration result is sealed and cannot be retried blindly."""
    calls: list[object] = []

    def failed_child(*_args: object, **_kwargs: object) -> SimpleNamespace:
        calls.append(None)
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(sample_cli.subprocess, "run", failed_child)

    first_result = sample_cli.main(["migrate", "--target", str(isolated_target.directory)])

    assert first_result == 3
    assert json.loads(capsys.readouterr().out) == {
        "status": "blocked",
        "code": "migration_outcome_uncertain",
    }
    sealed = sample.load_sample_target(isolated_target.directory)
    assert sealed.phase == "migration_uncertain"
    assert not (sealed.directory / sample.SAMPLE_LOCK_FILENAME).exists()
    assert isolated_target.database_path.stat().st_size == 0

    retry_result = sample_cli.main(["migrate", "--target", str(sealed.directory)])

    assert retry_result == 3
    assert json.loads(capsys.readouterr().out) == {
        "status": "blocked",
        "code": "migration_target_not_fresh",
    }
    assert len(calls) == 1
    assert sample.load_sample_target(sealed.directory).phase == "migration_uncertain"


def test_source_gate_migration_config_is_ascii_compatible() -> None:
    """Keep Alembic ini readable when its locale-based decoder is ASCII."""
    config_path = sample_cli.PROJECT_ROOT / "alembic_source_gate.ini"

    config_path.read_bytes().decode("ascii")


def test_migration_state_rejects_any_warehouse_version_marker(
    isolated_target: sample.SampleTarget,
) -> None:
    """A manifest flag cannot disguise an applied or partial warehouse chain."""
    with closing(sqlite3.connect(isolated_target.database_path)) as connection, connection:
        connection.execute("CREATE TABLE alembic_version_data (version_num VARCHAR(32) NOT NULL)")
        connection.execute("INSERT INTO alembic_version_data VALUES ('synthetic-warehouse-head')")

    with pytest.raises(sample.Csi500SampleCompositionError) as error:
        sample._read_migration_state(isolated_target)

    assert error.value.code == "warehouse_schema_must_remain_unapplied"


def _readonly_sqlite_snapshot(path: Path) -> tuple[bytes, str, tuple[str, ...]]:
    """Capture database bytes, integrity result, schema, and data without writes."""
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        dump = tuple(connection.iterdump())
    finally:
        connection.close()
    return path.read_bytes(), integrity, dump


@pytest.mark.parametrize("existing_state", ["unmarked-v1", "marked-head"])
def test_migration_refuses_existing_gate_state_without_database_changes(
    existing_state: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fresh-only composition migration preserves valid pre-existing gate databases."""
    target = sample.create_sample_target()
    try:
        if existing_state == "unmarked-v1":
            from sqlalchemy import create_engine

            from alembic_source_gate import schema_contract

            engine = create_engine(f"sqlite+pysqlite:///{target.database_path}")
            try:
                with engine.begin() as connection:
                    schema_contract._apply_v1_reference(connection)
                    schema_contract.validate_v1_schema(connection)
            finally:
                engine.dispose()
            with closing(sqlite3.connect(target.database_path)) as connection:
                assert (
                    connection.execute(
                        "SELECT name FROM sqlite_master WHERE name = 'alembic_version_source_gate'"
                    ).fetchone()
                    is None
                )
        else:
            from scripts import migrate_source_gate

            assert (
                migrate_source_gate.main(
                    ["--database-path", str(target.database_path), "--initialize-empty"]
                )
                == 0
            )
            with closing(sqlite3.connect(target.database_path)) as connection:
                assert connection.execute(
                    "SELECT version_num FROM alembic_version_source_gate"
                ).fetchone() == (sample.SAMPLE_SOURCE_GATE_MIGRATION_HEAD,)

        original_migration_environment = sample_cli._migration_environment

        def isolated_test_migration_environment(
            current: sample.SampleTarget,
        ) -> dict[str, str]:
            """Add only fixed host compatibility values to the real allowlist."""
            environment = original_migration_environment(current)
            assert environment["HOME"] == str(current.directory)
            environment.update(
                {
                    "USER": "od-test",
                    "CRYPTOGRAPHY_OPENSSL_NO_LEGACY": "1",
                }
            )
            return environment

        monkeypatch.setattr(
            sample_cli, "_migration_environment", isolated_test_migration_environment
        )
        before = _readonly_sqlite_snapshot(target.database_path)
        assert before[1] == "ok"

        result = sample_cli.main(["migrate", "--target", str(target.directory)])

        assert result == 3
        assert json.loads(capsys.readouterr().out) == {
            "status": "blocked",
            "code": "migration_outcome_uncertain",
        }
        sealed = sample.load_sample_target(target.directory)
        assert sealed.phase == "migration_uncertain"
        assert _readonly_sqlite_snapshot(target.database_path) == before
        assert not (target.directory / sample.SAMPLE_LOCK_FILENAME).exists()
    finally:
        _remove_test_owned_sample_target(target)


def _remove_test_owned_sample_target(target: sample.SampleTarget) -> None:
    """Remove only the exact private target created by the integration test."""
    current = sample.load_sample_target(target.directory)
    if current.identity != target.identity or current.directory.parent != Path("/tmp"):
        raise AssertionError("integration test target identity changed; refusing cleanup")
    allowed_names = {
        sample.SAMPLE_DATABASE_FILENAME,
        sample.SAMPLE_MANIFEST_FILENAME,
        sample.SAMPLE_LOCK_FILENAME,
        f"{sample.SAMPLE_DATABASE_FILENAME}-wal",
        f"{sample.SAMPLE_DATABASE_FILENAME}-shm",
        f"{sample.SAMPLE_DATABASE_FILENAME}-journal",
    }
    names = {item.name for item in current.directory.iterdir()}
    if not names.issubset(allowed_names):
        raise AssertionError("integration test target contains unexpected files; refusing cleanup")
    for name in sorted(names):
        path = current.directory / name
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise AssertionError(
                "integration test target contains an unsafe file; refusing cleanup"
            )
        if metadata.st_uid != os.getuid():
            raise AssertionError("integration test target ownership changed; refusing cleanup")
        path.unlink()
    current.directory.rmdir()


def test_create_target_is_private_and_identity_pinned(isolated_target: sample.SampleTarget) -> None:
    """The created database and manifest are private, regular files."""
    loaded = sample.load_sample_target(isolated_target.directory)
    database_stat = loaded.database_path.lstat()
    manifest_stat = (loaded.directory / sample.SAMPLE_MANIFEST_FILENAME).lstat()

    assert loaded.identity == isolated_target.identity
    assert loaded.phase == "created"
    assert stat.S_ISREG(database_stat.st_mode)
    assert stat.S_IMODE(database_stat.st_mode) == 0o600
    assert stat.S_IMODE(manifest_stat.st_mode) == 0o600
    assert database_stat.st_uid == os.getuid()


def test_sample_identity_digest_matches_fixed_golden_vector() -> None:
    """Device/inode identity serialization stays byte-for-byte stable."""
    identity = sample.DatabaseFileIdentity(device=123, inode=456)

    assert sample._sample_identity_sha256(identity) == (
        "b13da7a6a942b0bb74712b841f6a923dc17bd663e5985dde87153b4476034faf"
    )


def test_target_loader_rejects_identity_swap(isolated_target: sample.SampleTarget) -> None:
    """Replacing the database inode does not inherit the prior target state."""
    isolated_target.database_path.unlink()
    isolated_target.database_path.write_bytes(b"")
    isolated_target.database_path.chmod(0o600)

    with pytest.raises(sample.Csi500SampleCompositionError) as error:
        sample.load_sample_target(isolated_target.directory)

    assert error.value.code == "target_database_identity_invalid"


def test_target_transition_rejects_unbounded_nested_values(
    isolated_target: sample.SampleTarget,
) -> None:
    """Manifest updates remain bounded scalar metadata."""
    with pytest.raises(sample.Csi500SampleCompositionError) as error:
        sample.transition_sample_target(
            isolated_target,
            expected_phase="created",
            new_phase="migration_started",
            updates={"grant_id": {"unexpected": "nested"}},
        )

    assert error.value.code == "target_state_value_invalid"


def test_legacy_candidate_is_preserved_and_refused_without_rewrite(
    isolated_target: sample.SampleTarget,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Old dual-chain candidates remain byte-for-byte intact and read-only."""
    legacy_state = dict(isolated_target.state)
    legacy_state["schema_version"] = 1
    legacy_state.pop("target_profile")
    legacy_state.pop("warehouse_schema_status")
    legacy_state.update(
        {
            "phase": "intent_prepared",
            "capture_intent_id": "legacy-candidate-preserved",
            "one_shot_scope_status": "CANDIDATE_NOT_LOCKED",
        }
    )
    sample._write_manifest_atomically(isolated_target.directory, legacy_state)
    legacy = sample.load_sample_target(isolated_target.directory)
    manifest_path = legacy.directory / sample.SAMPLE_MANIFEST_FILENAME
    before_manifest = manifest_path.read_bytes()
    before_database = legacy.database_path.read_bytes()

    migration_result = sample_cli.main(["migrate", "--target", str(legacy.directory)])
    assert migration_result == 3
    assert json.loads(capsys.readouterr().out) == {
        "status": "blocked",
        "code": "legacy_target_profile_read_only",
    }

    evidence = sample.Csi500TradingDayEvidence(
        business_date=date.today() - timedelta(days=10),
        calendar_version="legacy-fixture",
        source_reference="offline fixture; not an authority proof",
        reviewed_by="offline-reviewer",
        manifest_sha256="a" * 64,
        artifact_sha256="b" * 64,
    )
    with pytest.raises(sample.Csi500SampleCompositionError) as error:
        sample.prepare_csi500_sample_one_shot_intent(
            legacy,
            calendar_evidence=evidence,
        )

    assert error.value.code == "legacy_target_profile_read_only"
    assert manifest_path.read_bytes() == before_manifest
    assert legacy.database_path.read_bytes() == before_database
    assert legacy.directory.is_dir()
    assert not (legacy.directory / sample.SAMPLE_LOCK_FILENAME).exists()


def test_target_lock_is_exclusive(isolated_target: sample.SampleTarget) -> None:
    """A second operation cannot enter while the private lock exists."""
    with sample.lock_sample_target(isolated_target, operation="authorization"):  # noqa: SIM117
        with pytest.raises(sample.Csi500SampleCompositionError) as error:
            with sample.lock_sample_target(isolated_target, operation="prepare"):
                pytest.fail("the second operation acquired an occupied lock")

    assert error.value.code == "target_locked_or_lock_unavailable"


def test_calendar_evidence_requires_exact_pinned_historical_session(tmp_path: Path) -> None:
    """The manifest pin is checked, and the requested date must be listed."""
    sample_date = date.today() - timedelta(days=10)
    manifest = _write_calendar_evidence(tmp_path, sample_date)

    evidence = sample.load_csi500_trading_day_evidence(
        manifest,
        expected_business_date=sample_date,
    )

    assert evidence.business_date == sample_date
    assert len(evidence.manifest_sha256) == 64
    assert len(evidence.artifact_sha256) == 64
    with pytest.raises(sample.Csi500SampleCompositionError) as error:
        sample.load_csi500_trading_day_evidence(
            manifest,
            expected_business_date=sample_date - timedelta(days=1),
        )
    assert error.value.code == "sample_date_not_in_calendar"


def test_calendar_evidence_rejects_artifact_digest_mismatch(tmp_path: Path) -> None:
    """Changing the pinned local artifact is detected before authorization."""
    sample_date = date.today() - timedelta(days=10)
    manifest = _write_calendar_evidence(tmp_path, sample_date)
    manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
    Path(manifest_value["artifact_path"]).write_text("changed\n", encoding="utf-8")

    with pytest.raises(sample.Csi500SampleCompositionError) as error:
        sample.load_csi500_trading_day_evidence(manifest, expected_business_date=sample_date)

    assert error.value.code == "calendar_artifact_pin_mismatch"


def test_calendar_evidence_rejects_boolean_schema_version(tmp_path: Path) -> None:
    """JSON booleans must not pass the integer schema version check."""
    business_date = date.today() - timedelta(days=10)
    manifest = _write_calendar_evidence(tmp_path, business_date)
    manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
    manifest_value["schema_version"] = True
    manifest.write_text(
        json.dumps(manifest_value, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    with pytest.raises(sample.Csi500SampleCompositionError) as error:
        sample.load_csi500_trading_day_evidence(manifest, expected_business_date=business_date)

    assert error.value.code == "calendar_manifest_shape_invalid"


def test_cli_exposes_only_authorization_and_offline_preparation_commands() -> None:
    """Completion and preflight commands are absent until their lifecycle is accepted."""
    command_action = next(
        action for action in sample_cli.build_parser()._actions if action.dest == "command"
    )

    assert set(command_action.choices) == {
        "_migration-child",
        "authorize",
        "init",
        "migrate",
        "prepare",
        "preview",
    }


def test_prepared_candidate_has_no_execution_success_transition() -> None:
    """The local lifecycle cannot record an unperformed provider execution."""
    assert sample._VALID_TRANSITIONS["intent_prepared"] == frozenset()
    assert "execution_succeeded" not in sample._PHASES
    assert "verified" not in sample._PHASES


def test_local_authorization_requires_a_current_lock_before_gate_reads(
    isolated_target: sample.SampleTarget,
) -> None:
    """Grant provisioning cannot be called outside the explicit target lock."""
    evidence = sample.Csi500TradingDayEvidence(
        business_date=date(2026, 1, 2),
        calendar_version="fixture-1",
        source_reference="offline fixture",
        reviewed_by="offline-reviewer",
        manifest_sha256="a" * 64,
        artifact_sha256="b" * 64,
    )

    current = sample.transition_sample_target(
        isolated_target,
        expected_phase="created",
        new_phase="migration_started",
    )
    current = sample.transition_sample_target(
        current,
        expected_phase="migration_started",
        new_phase="source_gate_initialized",
    )

    with pytest.raises(sample.Csi500SampleCompositionError) as error:
        sample.provision_sample_source_authorization(
            current,
            calendar_evidence=evidence,
            actor="offline-operator",
            principal_scope="local-csi500-sample-test",
            confirm_user_declared_internal_use=True,
        )

    assert error.value.code == "target_operation_lock_required"
    assert sample.load_sample_target(current.directory).phase == "source_gate_initialized"
    assert isolated_target.database_path.stat().st_size == 0


def test_invalid_authorization_confirmation_is_rejected_before_gate_reads(
    isolated_target: sample.SampleTarget,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing user declaration is rejected before touching migration or gate state."""
    current = sample.transition_sample_target(
        isolated_target,
        expected_phase="created",
        new_phase="migration_started",
    )
    current = sample.transition_sample_target(
        current,
        expected_phase="migration_started",
        new_phase="source_gate_initialized",
    )
    evidence = sample.Csi500TradingDayEvidence(
        business_date=date(2026, 10, 30),
        calendar_version="fixture-1",
        source_reference="offline fixture; not an authority proof",
        reviewed_by="offline-reviewer",
        manifest_sha256="a" * 64,
        artifact_sha256="b" * 64,
    )

    def fail_if_gate_is_read(_target: sample.SampleTarget) -> tuple[str, str]:
        pytest.fail("gate state must not be read without explicit user confirmation")

    monkeypatch.setattr(sample, "_read_migration_state", fail_if_gate_is_read)
    with pytest.raises(sample.Csi500SampleCompositionError) as error:
        sample.provision_sample_source_authorization(
            current,
            calendar_evidence=evidence,
            actor="offline-operator",
            principal_scope="local-research-service",
            confirm_user_declared_internal_use=False,
        )

    assert error.value.code == "internal_use_confirmation_required"
    assert sample.load_sample_target(current.directory).phase == "source_gate_initialized"
    assert isolated_target.database_path.stat().st_size == 0


def test_fresh_gate_check_requires_clock_singleton_and_rejects_residual_rows(
    isolated_target: sample.SampleTarget,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Allow the required clock row but reject other residual gate data."""
    monkeypatch.setattr(
        sample,
        "_read_migration_state",
        lambda _target: sample.SAMPLE_EXPECTED_MIGRATION_STATE,
    )
    with closing(sqlite3.connect(isolated_target.database_path)) as connection, connection:
        connection.execute(
            "CREATE TABLE od_source_gate_clock (clock_id INTEGER, highwater_us INTEGER)"
        )
        connection.execute("INSERT INTO od_source_gate_clock VALUES (1, 0)")
        for table_name in sample._FRESH_SAMPLE_GATE_TABLES:
            if table_name == "od_source_gate_clock":
                continue
            connection.execute(f"CREATE TABLE {table_name} (marker INTEGER)")

    # The required singleton is accepted; a valid row in another table is not.
    assert sample._require_fresh_sample_gate(isolated_target) is None
    with closing(sqlite3.connect(isolated_target.database_path)) as connection, connection:
        connection.execute("DELETE FROM od_source_gate_clock")
    with pytest.raises(sample.Csi500SampleCompositionError) as missing_clock_error:
        sample._require_fresh_sample_gate(isolated_target)
    assert missing_clock_error.value.code == "source_gate_clock_invalid"

    with closing(sqlite3.connect(isolated_target.database_path)) as connection, connection:
        connection.execute("INSERT INTO od_source_gate_clock VALUES (1, 0)")
        connection.execute("INSERT INTO od_source_gate_clock VALUES (2, 0)")
    with pytest.raises(sample.Csi500SampleCompositionError) as duplicate_clock_error:
        sample._require_fresh_sample_gate(isolated_target)
    assert duplicate_clock_error.value.code == "source_gate_clock_invalid"

    with closing(sqlite3.connect(isolated_target.database_path)) as connection, connection:
        connection.execute("DELETE FROM od_source_gate_clock WHERE clock_id = 2")
        connection.execute("INSERT INTO od_source_one_shot_configs VALUES (1)")

    with pytest.raises(sample.Csi500SampleCompositionError) as error:
        sample._require_fresh_sample_gate(isolated_target)

    assert error.value.code == "source_gate_target_not_fresh"


def test_local_authorization_records_only_after_grant_quota_and_readback(
    isolated_target: sample.SampleTarget,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The authorized phase follows both scoped writes and actual-state checks."""
    current = sample.transition_sample_target(
        isolated_target,
        expected_phase="created",
        new_phase="migration_started",
    )
    current = sample.transition_sample_target(
        current,
        expected_phase="migration_started",
        new_phase="source_gate_initialized",
    )
    grant_requests, quota_requests, readbacks = _patch_local_authorization_writers(
        monkeypatch,
        target=current,
    )
    evidence = sample.Csi500TradingDayEvidence(
        business_date=date(2026, 10, 30),
        calendar_version="fixture-1",
        source_reference="offline fixture; not an authority proof",
        reviewed_by="offline-reviewer",
        manifest_sha256="a" * 64,
        artifact_sha256="b" * 64,
    )

    with sample.lock_sample_target(current, operation="authorization"):
        authorized = sample.provision_sample_source_authorization(
            current,
            calendar_evidence=evidence,
            actor="offline-operator",
            principal_scope="local-research-service",
            confirm_user_declared_internal_use=True,
        )

    assert authorized.phase == "authorized"
    assert authorized.state["source_rights_basis"] == "USER_DECLARED_UNVERIFIED"
    assert authorized.state["quota_target_identity_sha256"] == sample._sample_identity_sha256(
        current.identity
    )
    assert len(grant_requests) == 1
    assert len(quota_requests) == 1
    assert readbacks == ["rows", "gate"]
    assert grant_requests[0].principal_scope == "local-research-service"
    assert grant_requests[0].operation == "query"
    assert quota_requests[0]["database_path"] == current.database_path


def test_cli_real_migrations_authorize_and_prepare_on_private_sqlite_target(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise target-cwd migrations and local gate setup without a provider request."""
    original_migration_environment = sample_cli._migration_environment

    def isolated_test_migration_environment(
        target: sample.SampleTarget,
    ) -> dict[str, str]:
        """Add only test-host compatibility values to the real migration allowlist."""
        environment = original_migration_environment(target)
        assert environment["PYTHONUTF8"] == "1"
        assert environment["HOME"] == str(target.directory)
        environment.update(
            {
                "USER": "od-test",
                "CRYPTOGRAPHY_OPENSSL_NO_LEGACY": "1",
            }
        )
        return environment

    # The isolated test host's passwd/OpenSSL setup differs from production.
    # These synthetic values unblock only this test subprocess and do not claim
    # that the production host environment has been exercised or validated.
    monkeypatch.setattr(sample_cli, "_migration_environment", isolated_test_migration_environment)
    target = sample.create_sample_target()
    business_date = date(2026, 10, 30)
    calendar_manifest = _write_calendar_evidence(tmp_path, business_date)
    try:
        assert target.directory.parent == Path("/tmp")
        assert target.directory != Path(__file__).resolve().parents[1]
        # `migrate` starts its child with this private target as cwd.
        assert sample_cli.main(["migrate", "--target", str(target.directory)]) == 0
        migration_output = json.loads(capsys.readouterr().out)
        assert migration_output == {
            "database_identity": "pinned",
            "source_gate_head": sample.SAMPLE_SOURCE_GATE_MIGRATION_HEAD,
            "status": "source_gate_initialized",
            "target_id": target.state["target_id"],
            "target_profile": sample.SAMPLE_TARGET_PROFILE,
            "warehouse_schema_status": "NOT_APPLIED",
        }
        initialized = sample.load_sample_target(target.directory)
        assert initialized.phase == "source_gate_initialized"
        assert initialized.state["warehouse_schema_status"] == "NOT_APPLIED"
        assert sample._read_migration_state(initialized) == sample.SAMPLE_EXPECTED_MIGRATION_STATE
        assert sample._require_fresh_sample_gate(initialized) is None
        with closing(sqlite3.connect(target.database_path)) as connection:
            table_names = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name NOT LIKE 'sqlite_%'"
                )
            }
            assert table_names == {
                *sample._FRESH_SAMPLE_GATE_TABLES,
                sample.SAMPLE_SOURCE_GATE_VERSION_TABLE,
            }
            assert "alembic_version_data" not in table_names
            assert "alembic_version" not in table_names

        assert (
            sample_cli.main(
                [
                    "authorize",
                    "--target",
                    str(target.directory),
                    "--calendar-manifest",
                    str(calendar_manifest),
                    "--business-date",
                    business_date.isoformat(),
                    "--actor",
                    "synthetic-integration-operator",
                    "--principal-scope",
                    "synthetic-research-service",
                    "--confirm-user-declared-internal-use",
                ]
            )
            == 0
        )
        authorization_output = json.loads(capsys.readouterr().out)
        assert authorization_output["status"] == "authorized_for_review_only"
        assert authorization_output["target_id"] == target.state["target_id"]
        assert isinstance(authorization_output["grant_id"], str)
        assert authorization_output["rights_basis"] == "USER_DECLARED_UNVERIFIED"
        assert authorization_output["rights_independently_verified"] is False
        assert authorization_output["local_grant_configured"] is True
        assert authorization_output["local_quota_configured"] is True
        assert authorization_output["live_execution_enabled"] is False
        authorized = sample.load_sample_target(target.directory)
        assert authorized.phase == "authorized"
        assert authorized.state["source_rights_basis"] == "USER_DECLARED_UNVERIFIED"
        assert authorization_output["grant_id"] == authorized.state["grant_id"]
        sample._validate_authorization_configuration(authorized)
        with pytest.raises(sample.Csi500SampleCompositionError) as freshness_error:
            sample._require_fresh_sample_gate(authorized)
        assert freshness_error.value.code == "source_gate_target_not_fresh"

        assert (
            sample_cli.main(
                [
                    "prepare",
                    "--target",
                    str(target.directory),
                    "--calendar-manifest",
                    str(calendar_manifest),
                    "--business-date",
                    business_date.isoformat(),
                ]
            )
            == 0
        )
        preparation_output = json.loads(capsys.readouterr().out)
        assert preparation_output["status"] == "intent_prepared"
        assert preparation_output["live_execution_enabled"] is False
        assert preparation_output["source_rights_basis"] == "USER_DECLARED_UNVERIFIED"
        prepared = sample.load_sample_target(target.directory)
        assert prepared.phase == "intent_prepared"
        assert prepared.state["one_shot_scope_status"] == "CANDIDATE_NOT_LOCKED"

        connection = sample._connect_read_only(target.database_path)
        try:
            assert connection.execute("SELECT COUNT(*) FROM od_source_grants").fetchone()[0] == 1
            assert (
                connection.execute("SELECT COUNT(*) FROM od_source_grant_hosts").fetchone()[0] == 1
            )
            assert (
                connection.execute("SELECT COUNT(*) FROM od_source_grant_host_sets").fetchone()[0]
                == 1
            )
            assert (
                connection.execute("SELECT COUNT(*) FROM od_source_grant_events").fetchone()[0] == 1
            )
            assert (
                connection.execute("SELECT COUNT(*) FROM od_source_gate_policies").fetchone()[0]
                == 1
            )
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM od_source_gate_source_bindings"
                ).fetchone()[0]
                == 1
            )
            assert (
                connection.execute("SELECT COUNT(*) FROM od_source_gate_state").fetchone()[0] == 1
            )
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM od_source_attempt_reservations"
                ).fetchone()[0]
                == 0
            )
            assert (
                connection.execute("SELECT COUNT(*) FROM od_source_one_shot_configs").fetchone()[0]
                == 0
            )
            assert (
                connection.execute("SELECT COUNT(*) FROM od_source_one_shot_permits").fetchone()[0]
                == 0
            )
        finally:
            connection.close()

        with closing(sqlite3.connect(target.database_path)) as connection, connection:
            connection.execute(
                "UPDATE od_source_gate_state SET tokens_micro = 0 WHERE quota_key = ?",
                (sample.SAMPLE_QUOTA_KEY,),
            )
        assert (
            sample_cli.main(
                [
                    "prepare",
                    "--target",
                    str(target.directory),
                    "--calendar-manifest",
                    str(calendar_manifest),
                    "--business-date",
                    business_date.isoformat(),
                ]
            )
            == 3
        )
        tamper_output = json.loads(capsys.readouterr().out)
        assert tamper_output == {
            "code": "source_gate_configuration_mismatch",
            "status": "blocked",
        }
        assert sample.load_sample_target(target.directory).phase == "intent_prepared"
    finally:
        _remove_test_owned_sample_target(target)


def test_uncertain_quota_commit_seals_target_against_authorization_retry(
    isolated_target: sample.SampleTarget,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A grant may already exist, so an uncertain quota write seals the target."""
    current = sample.transition_sample_target(
        isolated_target,
        expected_phase="created",
        new_phase="migration_started",
    )
    current = sample.transition_sample_target(
        current,
        expected_phase="migration_started",
        new_phase="source_gate_initialized",
    )
    grant_requests, quota_requests, _readbacks = _patch_local_authorization_writers(
        monkeypatch,
        target=current,
        quota_error=RuntimeError("synthetic commit uncertainty"),
    )
    evidence = sample.Csi500TradingDayEvidence(
        business_date=date(2026, 10, 30),
        calendar_version="fixture-1",
        source_reference="offline fixture; not an authority proof",
        reviewed_by="offline-reviewer",
        manifest_sha256="a" * 64,
        artifact_sha256="b" * 64,
    )

    with (
        sample.lock_sample_target(current, operation="authorization"),
        pytest.raises(sample.Csi500SampleCompositionError) as error,
    ):
        sample.provision_sample_source_authorization(
            current,
            calendar_evidence=evidence,
            actor="offline-operator",
            principal_scope="local-research-service",
            confirm_user_declared_internal_use=True,
        )

    sealed = sample.load_sample_target(current.directory)
    assert error.value.code == "authorization_outcome_uncertain"
    assert sealed.phase == "authorization_started"
    assert len(grant_requests) == len(quota_requests) == 1
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
    assert len(grant_requests) == len(quota_requests) == 1


def test_cli_authorize_refuses_target_without_actual_source_gate_schema(
    isolated_target: sample.SampleTarget,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A phase label alone cannot authorize grant or quota writes."""
    current = sample.transition_sample_target(
        isolated_target,
        expected_phase="created",
        new_phase="migration_started",
    )
    current = sample.transition_sample_target(
        current,
        expected_phase="migration_started",
        new_phase="source_gate_initialized",
    )
    calendar_manifest = _write_calendar_evidence(tmp_path, date(2026, 10, 30))

    result = sample_cli.main(
        [
            "authorize",
            "--target",
            str(current.directory),
            "--calendar-manifest",
            str(calendar_manifest),
            "--business-date",
            "2026-10-30",
            "--actor",
            "offline-operator",
            "--principal-scope",
            "local-csi500-sample-test",
            "--confirm-user-declared-internal-use",
        ]
    )

    assert result == 3
    assert json.loads(capsys.readouterr().out) == {
        "status": "blocked",
        "code": "source_gate_schema_mismatch",
    }
    assert sample.load_sample_target(current.directory).phase == "source_gate_initialized"
    assert current.database_path.stat().st_size == 0


def test_preview_cli_is_user_reachable_without_target_or_authorization(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Preview exposes only the fixed request shape and never touches runtime state."""
    for name in (
        "create_sample_target",
        "load_sample_target",
        "load_csi500_trading_day_evidence",
        "_connect_read_only",
    ):
        monkeypatch.setattr(
            sample,
            name,
            lambda *_args, _name=name, **_kwargs: pytest.fail(
                f"preview unexpectedly called {_name}"
            ),
        )

    result = sample_cli.main(["preview", "--business-date", "2026-10-30"])

    output = json.loads(capsys.readouterr().out)
    assert result == 0
    assert output == {
        "authorization_status": "not_established",
        "business_date": "2026-10-30",
        "calendar_evidence_status": "not_verified",
        "dataset_id": sample.SAMPLE_DATASET_ID,
        "execution_enabled": False,
        "gate_status": "not_accessed",
        "request": {
            "host": "fuyao.aicubes.cn",
            "method": "GET",
            "path": "/api/a-share-index/prices/historical",
            "query_summary_sha256": output["request"]["query_summary_sha256"],
        },
        "status": "preview_only",
        "symbol": "000905.SH",
        "target_status": "not_required_not_accessed",
    }
    assert len(output["request"]["query_summary_sha256"]) == 64


def test_preview_does_not_claim_calendar_or_authorization() -> None:
    """The offline request-shape preview marks its unverified inputs explicitly."""
    preview = sample.preview_csi500_sample_request(business_date=date(2026, 10, 30))

    assert preview.status == "preview_only"
    assert preview.authorization_status == "not_established"
    assert preview.calendar_evidence_status == "not_verified"
    assert preview.target_status == "not_required_not_accessed"
    assert preview.gate_status == "not_accessed"
    assert preview.execution_enabled is False


def _build_v4_spec(business_date: date, *, artifact_sha256: str = "b" * 64):
    """Build only deterministic scalar intent data; no target or provider is opened."""
    return build_csi500_v4_sample_spec(
        business_date=business_date,
        target_id="1" * 32,
        target_identity_sha256="a" * 64,
        calendar_manifest_sha256="c" * 64,
        calendar_artifact_sha256=artifact_sha256,
        authorization_actor="offline-operator",
        grant_id="csi500-sample-test",
        grant_revision=1,
        principal_scope="local-csi500-sample-test",
        grant_expires_at_utc="2026-10-31T10:15:00Z",
        rights_evidence_sha256="d" * 64,
        budget_binding_sha256="e" * 64,
        budget_quota_key="csi500-fuyao-sample-204",
        budget_policy_revision=1,
        budget_policy_sha256="f" * 64,
    )


def test_v4_intent_is_deterministic_and_pins_one_exact_request() -> None:
    """The offline candidate fixes symbol, route, one date and all stable IDs."""
    business_date = date.today() - timedelta(days=10)
    first = _build_v4_spec(business_date)
    again = _build_v4_spec(business_date)
    changed_calendar = _build_v4_spec(business_date, artifact_sha256="b" * 63 + "c")

    assert first == again
    assert first.capture_intent_id == first.request.idempotency_key
    assert first.run_key == first.source_attempt_scope.attempt.run_key
    assert first.attempt_id == first.source_attempt_scope.attempt.attempt_id
    assert first.request_id == first.source_attempt_scope.attempt.request_id
    assert first.source_attempt_scope.attempt.attempt_no == 1
    assert first.request.partition == {"symbol": sample.SAMPLE_SYMBOL}
    assert first.request.query == {
        "start_date": business_date.isoformat(),
        "end_date": business_date.isoformat(),
    }
    assert first.source_attempt_scope.request_scheme == "https"
    assert first.source_attempt_scope.request_method == "GET"
    assert first.source_attempt_scope.request_host == "fuyao.aicubes.cn"
    assert first.source_attempt_scope.request_path == "/api/a-share-index/prices/historical"
    assert first.source_attempt_scope_v3_pin.request_summary_allowed_fields == (
        "end",
        "interval",
        "start",
        "thscode",
    )
    assert first.task_definition.enabled is False
    assert first.task_definition.schedule is None
    assert first.intent_sha256 != changed_calendar.intent_sha256
    payload = build_csi500_v4_sample_payload(spec=first)
    assert payload["query"] == first.request.query
    assert payload["partition"] == {"symbol": sample.SAMPLE_SYMBOL}
    assert "key=<redacted>" not in repr(first)


def _synthetically_authorize_manifest(
    target: sample.SampleTarget,
    evidence: sample.Csi500TradingDayEvidence,
) -> sample.SampleTarget:
    """Create only a local fixture manifest; this deliberately does not migrate SQLite."""
    current = sample.transition_sample_target(
        target,
        expected_phase="created",
        new_phase="migration_started",
    )
    current = sample.transition_sample_target(
        current,
        expected_phase="migration_started",
        new_phase="source_gate_initialized",
    )
    current = sample.transition_sample_target(
        current,
        expected_phase="source_gate_initialized",
        new_phase="authorization_started",
        updates={
            "calendar_date": evidence.business_date.isoformat(),
            "calendar_manifest_sha256": evidence.manifest_sha256,
            "calendar_artifact_sha256": evidence.artifact_sha256,
            "calendar_source_reference": "offline fixture; not an authority proof",
            "calendar_version": "fixture-1",
            "calendar_reviewed_by": "offline-reviewer",
            "authorization_actor": "offline-operator",
            "source_rights_basis": "USER_DECLARED_UNVERIFIED",
            "grant_id": "csi500-sample-test",
            "principal_scope": "local-csi500-sample-test",
            "grant_expires_at_utc": (datetime.now(timezone.utc) + timedelta(minutes=5))
            .isoformat()
            .replace("+00:00", "Z"),
            "rights_evidence_sha256": "d" * 64,
        },
    )
    return sample.transition_sample_target(
        current,
        expected_phase="authorization_started",
        new_phase="authorized",
        updates={
            "quota_key": sample.SAMPLE_QUOTA_KEY,
            "quota_policy_revision": 1,
            "quota_policy_sha256": "f" * 64,
            "quota_binding_sha256": "e" * 64,
            "quota_target_identity_sha256": sample._sample_identity_sha256(target.identity),
        },
    )


def test_authorization_readback_rejects_manifest_without_real_gate_rows(
    isolated_target: sample.SampleTarget,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A complete-looking receipt still fails when persisted grant rows are absent."""
    business_date = date.today() - timedelta(days=10)
    evidence = sample.Csi500TradingDayEvidence(
        business_date=business_date,
        calendar_version="fixture-1",
        source_reference="offline fixture; not an authority proof",
        reviewed_by="offline-reviewer",
        manifest_sha256="a" * 64,
        artifact_sha256="b" * 64,
    )
    authorized = _synthetically_authorize_manifest(isolated_target, evidence)
    monkeypatch.setattr(
        sample,
        "_read_migration_state",
        lambda _target: sample.SAMPLE_EXPECTED_MIGRATION_STATE,
    )

    with pytest.raises(sample.Csi500SampleCompositionError) as error:
        sample._validate_authorization_configuration(authorized)

    assert error.value.code == "source_gate_readback_unavailable"


@pytest.mark.parametrize(
    ("grant_status", "scope_status", "expected_error"),
    [
        ("MATCHED", "UNCONFIGURED", None),
        ("REJECTED", "UNCONFIGURED", "source_grant_readback_mismatch"),
        ("MATCHED", "CONSUMED", "one_shot_scope_not_unlocked"),
    ],
)
def test_gate_inspection_requires_active_grant_and_unlocked_scope(
    isolated_target: sample.SampleTarget,
    monkeypatch: pytest.MonkeyPatch,
    grant_status: str,
    scope_status: str,
    expected_error: str | None,
) -> None:
    """The gate inspector rejects mismatched or already-used local state."""
    from opendata.data import source_attempt_gate as gate_module

    business_date = date.today() - timedelta(days=10)
    evidence = sample.Csi500TradingDayEvidence(
        business_date=business_date,
        calendar_version="fixture-1",
        source_reference="offline fixture; not an authority proof",
        reviewed_by="offline-reviewer",
        manifest_sha256="a" * 64,
        artifact_sha256="b" * 64,
    )
    target = _synthetically_authorize_manifest(isolated_target, evidence)
    spec = sample._build_sample_v4_spec(target, evidence)
    created_engines: list[object] = []
    inspected_grants: list[object] = []
    inspected_scopes: list[object] = []

    class FakeEngine:
        disposed = False

        def dispose(self) -> None:
            self.disposed = True

    class FakeGate:
        def __init__(self, _engine: object) -> None:
            pass

        def inspect_grant(self, request: object) -> object:
            inspected_grants.append(request)
            status = gate_module.SourceGrantInspectionStatus[grant_status]
            reason = (
                gate_module.SourceGrantInspectionReason.GRANT_MATCHED
                if grant_status == "MATCHED"
                else gate_module.SourceGrantInspectionReason.GRANT_NOT_FOUND
            )
            return gate_module.SourceGrantInspectionResult(status=status, reason=reason)

        def inspect_one_shot_scope(self, scope: object) -> object:
            inspected_scopes.append(scope)
            status = gate_module.OneShotScopeInspectionStatus[scope_status]
            return gate_module.OneShotScopeInspection(status=status)

    def create_fake_engine(*_args: object, **_kwargs: object) -> FakeEngine:
        engine = FakeEngine()
        created_engines.append(engine)
        return engine

    monkeypatch.setattr(sample, "create_engine", create_fake_engine)
    monkeypatch.setattr(gate_module, "SourceAttemptGate", FakeGate)

    if expected_error is None:
        sample._inspect_sample_gate_configuration(target, scope=spec.source_attempt_scope)
    else:
        with pytest.raises(sample.Csi500SampleCompositionError) as error:
            sample._inspect_sample_gate_configuration(target, scope=spec.source_attempt_scope)
        assert error.value.code == expected_error

    assert len(inspected_grants) == 1
    assert inspected_grants[0].grant_id == target.state["grant_id"]
    assert inspected_grants[0].host == "fuyao.aicubes.cn"
    assert inspected_scopes == ([spec.source_attempt_scope] if grant_status == "MATCHED" else [])
    assert len(created_engines) == 1
    assert created_engines[0].disposed is True


def test_prepare_persists_only_offline_candidate_and_is_idempotent(
    isolated_target: sample.SampleTarget,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Intent preparation follows read-only authorization and gate verification."""
    business_date = date.today() - timedelta(days=10)
    evidence = sample.Csi500TradingDayEvidence(
        business_date=business_date,
        calendar_version="fixture-1",
        source_reference="offline fixture; not an authority proof",
        reviewed_by="offline-reviewer",
        manifest_sha256="a" * 64,
        artifact_sha256="b" * 64,
    )
    authorized = _synthetically_authorize_manifest(isolated_target, evidence)
    verified: list[str] = []
    monkeypatch.setattr(
        sample,
        "_read_migration_state",
        lambda _target: sample.SAMPLE_EXPECTED_MIGRATION_STATE,
    )
    monkeypatch.setattr(
        sample,
        "_validate_authorization_configuration",
        lambda _target, **_kwargs: verified.append("authorization"),
    )
    monkeypatch.setattr(
        sample,
        "_inspect_sample_gate_configuration",
        lambda _target, **_kwargs: verified.append("one-shot"),
    )

    first = sample.prepare_csi500_sample_one_shot_intent(
        authorized,
        calendar_evidence=evidence,
    )
    prepared = sample.load_sample_target(authorized.directory)
    second = sample.prepare_csi500_sample_one_shot_intent(
        authorized,
        calendar_evidence=evidence,
    )

    assert prepared.phase == "intent_prepared"
    assert prepared.state["one_shot_scope_status"] == "CANDIDATE_NOT_LOCKED"
    assert prepared.state["one_shot_intent_sha256"] == first.intent_sha256
    assert second == first
    assert verified == ["authorization", "one-shot", "authorization", "one-shot"]
    assert isolated_target.database_path.stat().st_size == 0


def test_prepare_rejects_synthetic_authorization_without_source_gate_schema(
    isolated_target: sample.SampleTarget,
) -> None:
    """A locally edited manifest cannot replace the actual source-gate schema."""
    business_date = date.today() - timedelta(days=10)
    evidence = sample.Csi500TradingDayEvidence(
        business_date=business_date,
        calendar_version="fixture-1",
        source_reference="offline fixture; not an authority proof",
        reviewed_by="offline-reviewer",
        manifest_sha256="a" * 64,
        artifact_sha256="b" * 64,
    )
    authorized = _synthetically_authorize_manifest(isolated_target, evidence)

    with pytest.raises(sample.Csi500SampleCompositionError) as error:
        sample.prepare_csi500_sample_one_shot_intent(
            authorized,
            calendar_evidence=evidence,
        )

    assert error.value.code == "source_gate_schema_mismatch"
    assert sample.load_sample_target(authorized.directory).phase == "authorized"
    assert isolated_target.database_path.stat().st_size == 0


def test_prepare_cli_checks_gate_before_reporting_candidate(
    isolated_target: sample.SampleTarget,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The CLI verifies target configuration and leaves live execution disabled."""
    business_date = date.today() - timedelta(days=10)
    calendar_manifest = _write_calendar_evidence(tmp_path, business_date)
    evidence = sample.load_csi500_trading_day_evidence(
        calendar_manifest,
        expected_business_date=business_date,
    )
    _synthetically_authorize_manifest(isolated_target, evidence)
    verified: list[str] = []
    monkeypatch.setattr(
        sample,
        "_read_migration_state",
        lambda _target: sample.SAMPLE_EXPECTED_MIGRATION_STATE,
    )
    monkeypatch.setattr(
        sample,
        "_validate_authorization_configuration",
        lambda _target, **_kwargs: verified.append("authorization"),
    )
    monkeypatch.setattr(
        sample,
        "_inspect_sample_gate_configuration",
        lambda _target, **_kwargs: verified.append("one-shot"),
    )
    monkeypatch.setattr(
        sample_cli,
        "_migration_environment",
        lambda _target: pytest.fail("offline prepare CLI read or constructed an environment"),
    )

    result = sample_cli.main(
        [
            "prepare",
            "--target",
            str(isolated_target.directory),
            "--calendar-manifest",
            str(calendar_manifest),
            "--business-date",
            business_date.isoformat(),
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert result == 0
    assert output["status"] == "intent_prepared"
    assert output["gate_scope_status"] == "CANDIDATE_NOT_LOCKED"
    assert output["live_execution_enabled"] is False
    assert output["symbol"] == sample.SAMPLE_SYMBOL
    assert verified == ["authorization", "one-shot"]
