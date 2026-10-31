"""Offline edge cases for CSI 500 source-gate readback and intent preparation."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import URL, create_engine
from sqlalchemy.pool import NullPool

from opendata.data.source_attempt_gate import ReservationStatus, SourceAttemptGate
from opendata.pipeline import ths_csi500_sample_composition as sample
from scripts.ops import run_csi500_one_shot_sample as sample_cli


_BUSINESS_DATE = date(2026, 10, 30)


def _write_calendar_evidence(directory: Path) -> Path:
    """Write a synthetic local calendar artifact for the isolated sample."""
    artifact = directory / "offline-sse-calendar.txt"
    artifact.write_text(
        f"SSE session: {_BUSINESS_DATE.isoformat()}\n",
        encoding="utf-8",
    )
    manifest = directory / "offline-calendar-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "exchange": "SSE",
                "calendar_version": "synthetic-fixture-v1",
                "source_reference": "synthetic fixture; not an authority proof",
                "retrieved_at_utc": "2026-10-30T00:00:00Z",
                "reviewed_by": "offline-test-fixture",
                "artifact_path": str(artifact),
                "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                "open_sessions": [_BUSINESS_DATE.isoformat()],
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    return manifest


def _remove_owned_target(target: sample.SampleTarget) -> None:
    """Remove only the exact private target created by this fixture."""
    if (
        sample.SAMPLE_TEMP_ROOT != Path("/tmp")
        or target.directory.parent != Path("/tmp")
        or not target.directory.name.startswith(sample.SAMPLE_TARGET_PREFIX)
    ):
        raise AssertionError("refusing to remove a target outside the private /tmp root")
    current = sample.load_sample_target(target.directory)
    if current.identity != target.identity:
        raise AssertionError("refusing to remove a target whose identity changed")
    sample._assert_sample_database_identity(current)
    shutil.rmtree(current.directory)


@pytest.fixture
def authorized_sample_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> Iterator[tuple[sample.SampleTarget, sample.Csi500TradingDayEvidence]]:
    """Create an authorized synthetic gate target under the private `/tmp` root."""
    original_environment = sample_cli._migration_environment

    def isolated_migration_environment(target: sample.SampleTarget) -> dict[str, str]:
        """Add only compatibility values for this isolated migration subprocess."""
        environment = original_environment(target)
        environment.update(
            {
                "USER": "od-test",
                "CRYPTOGRAPHY_OPENSSL_NO_LEGACY": "1",
            }
        )
        return environment

    monkeypatch.setattr(
        sample_cli,
        "_migration_environment",
        isolated_migration_environment,
    )
    target = sample.create_sample_target()
    try:
        assert target.directory.parent == Path("/tmp")
        assert sample_cli.main(["migrate", "--target", str(target.directory)]) == 0
        capsys.readouterr()
        manifest_path = _write_calendar_evidence(tmp_path)
        assert (
            sample_cli.main(
                [
                    "authorize",
                    "--target",
                    str(target.directory),
                    "--calendar-manifest",
                    str(manifest_path),
                    "--business-date",
                    _BUSINESS_DATE.isoformat(),
                    "--actor",
                    "offline-test-operator",
                    "--principal-scope",
                    "offline-csi500-test-worker",
                    "--confirm-user-declared-internal-use",
                ]
            )
            == 0
        )
        capsys.readouterr()
        authorized = sample.load_sample_target(target.directory)
        evidence = sample.load_csi500_trading_day_evidence(
            manifest_path,
            expected_business_date=_BUSINESS_DATE,
        )
        assert authorized.phase == "authorized"
        yield authorized, evidence
    finally:
        _remove_owned_target(target)


def test_prepare_rejects_a_mismatched_source_gate_head_before_authorization_readback(
    authorized_sample_target: tuple[
        sample.SampleTarget,
        sample.Csi500TradingDayEvidence,
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A validated but stale migration head cannot advance an authorized target."""
    target, evidence = authorized_sample_target
    stale_state = sample.SampleMigrationState(
        warehouse_schema_status=sample.SAMPLE_WAREHOUSE_SCHEMA_STATUS,
        source_gate_head="reviewed-but-stale-source-gate-head",
    )
    monkeypatch.setattr(sample, "_read_migration_state", lambda _target: stale_state)

    with pytest.raises(sample.Csi500SampleCompositionError) as freshness_error:
        sample._require_fresh_sample_gate(target)
    assert freshness_error.value.code == "migration_state_mismatch"

    with pytest.raises(sample.Csi500SampleCompositionError) as authorization_error:
        sample._validate_authorization_configuration(target)
    assert authorization_error.value.code == "migration_state_mismatch"

    monkeypatch.setattr(
        sample,
        "_validate_authorization_configuration",
        lambda *_args, **_kwargs: pytest.fail("stale schema must fail before grant readback"),
    )
    with pytest.raises(sample.Csi500SampleCompositionError) as error:
        sample.prepare_csi500_sample_one_shot_intent(
            target,
            calendar_evidence=evidence,
        )

    assert error.value.code == "migration_state_mismatch"
    assert sample.load_sample_target(target.directory).phase == "authorized"


def test_gate_readbacks_reject_a_stale_database_identity_before_opening_gate_state(
    authorized_sample_target: tuple[
        sample.SampleTarget,
        sample.Csi500TradingDayEvidence,
    ],
) -> None:
    """A stale target object cannot inspect or validate a different database inode."""
    target, evidence = authorized_sample_target
    stale_identity = sample.DatabaseFileIdentity(
        target.identity.device + 1,
        target.identity.inode,
    )
    stale_target = sample.SampleTarget(
        target.directory,
        target.database_path,
        stale_identity,
        target.phase,
        target.state,
    )
    spec = sample._build_sample_v4_spec(target, evidence)
    readbacks = (
        lambda: sample._require_fresh_sample_gate(stale_target),
        lambda: sample._validate_authorization_configuration(stale_target),
        lambda: sample._inspect_sample_gate_configuration(
            stale_target,
            scope=spec.source_attempt_scope,
        ),
    )

    for readback in readbacks:
        with pytest.raises(sample.Csi500SampleCompositionError) as error:
            readback()
        assert error.value.code == "target_identity_changed"


def test_readbacks_fail_closed_after_real_source_gate_schema_drift(
    authorized_sample_target: tuple[
        sample.SampleTarget,
        sample.Csi500TradingDayEvidence,
    ],
) -> None:
    """An extra view or missing quota table blocks local source-gate readback."""
    target, evidence = authorized_sample_target
    spec = sample._build_sample_v4_spec(target, evidence)
    with closing(sqlite3.connect(target.database_path)) as connection, connection:
        connection.execute("CREATE VIEW unexpected_gate_schema AS SELECT 1 AS value")

    with pytest.raises(sample.Csi500SampleCompositionError) as schema_error:
        sample._read_migration_state(target)
    assert schema_error.value.code == "source_gate_schema_mismatch"

    with closing(sqlite3.connect(target.database_path)) as connection, connection:
        connection.execute("DROP VIEW unexpected_gate_schema")
        connection.execute("DROP TABLE od_source_gate_state")

    with pytest.raises(sample.Csi500SampleCompositionError) as rows_error:
        sample._validate_source_gate_rows(target, allow_one_attempt=False)
    assert rows_error.value.code == "source_gate_readback_unavailable"

    with pytest.raises(sample.Csi500SampleCompositionError) as grant_error:
        sample._inspect_sample_gate_configuration(
            target,
            scope=spec.source_attempt_scope,
        )
    assert grant_error.value.code == "source_grant_readback_mismatch"


def test_readback_rejects_a_real_denied_gate_attempt(
    authorized_sample_target: tuple[
        sample.SampleTarget,
        sample.Csi500TradingDayEvidence,
    ],
) -> None:
    """A local gate denial is persisted and cannot be mistaken for a reserved call."""
    target, evidence = authorized_sample_target
    spec = sample.prepare_csi500_sample_one_shot_intent(
        target,
        calendar_evidence=evidence,
    )
    engine = create_engine(
        URL.create("sqlite", database=str(target.database_path)),
        poolclass=NullPool,
        connect_args={"timeout": 5.0},
    )
    try:
        gate = SourceAttemptGate(engine)
        denied_attempt = replace(
            spec.source_attempt_scope.attempt,
            host="unexpected.example",
        )
        reservation = gate.reserve_attempt(denied_attempt)
        assert reservation.status is ReservationStatus.DENIED

        with pytest.raises(sample.Csi500SampleCompositionError) as fresh_error:
            sample._validate_source_gate_rows(target, allow_one_attempt=False)
        assert fresh_error.value.code == "source_gate_target_not_fresh"

        with pytest.raises(sample.Csi500SampleCompositionError) as attempt_error:
            sample._validate_source_gate_rows(target, allow_one_attempt=True)
        assert attempt_error.value.code == "source_gate_attempt_mismatch"
    finally:
        engine.dispose()


def test_source_gate_readback_rejects_a_missing_quota_bucket(
    authorized_sample_target: tuple[
        sample.SampleTarget,
        sample.Csi500TradingDayEvidence,
    ],
) -> None:
    """Missing persisted quota state is incomplete authorization evidence."""
    target, _evidence = authorized_sample_target
    with closing(sqlite3.connect(target.database_path)) as connection, connection:
        connection.execute(
            "DELETE FROM od_source_gate_state WHERE quota_key = ?",
            (sample.SAMPLE_QUOTA_KEY,),
        )

    with pytest.raises(sample.Csi500SampleCompositionError) as error:
        sample._validate_source_gate_rows(target, allow_one_attempt=False)

    assert error.value.code == "source_gate_target_not_fresh"


def test_readback_rejects_an_unknown_source_gate_migration_marker(
    authorized_sample_target: tuple[
        sample.SampleTarget,
        sample.Csi500TradingDayEvidence,
    ],
) -> None:
    """An unrecognized marker blocks preparation instead of trusting phase text."""
    target, evidence = authorized_sample_target
    with closing(sqlite3.connect(target.database_path)) as connection, connection:
        connection.execute(
            f"UPDATE {sample.SAMPLE_SOURCE_GATE_VERSION_TABLE} SET version_num = ?",
            ("unreviewed-source-gate-revision",),
        )

    with pytest.raises(sample.Csi500SampleCompositionError) as error:
        sample.prepare_csi500_sample_one_shot_intent(
            target,
            calendar_evidence=evidence,
        )

    assert error.value.code == "migration_state_unavailable"
    assert sample.load_sample_target(target.directory).phase == "authorized"


def test_repeated_intent_preparation_is_stable_and_manifest_tampering_blocks_replay(
    authorized_sample_target: tuple[
        sample.SampleTarget,
        sample.Csi500TradingDayEvidence,
    ],
) -> None:
    """Preparation is idempotent, while a changed persisted intent hash fails closed."""
    target, evidence = authorized_sample_target
    first = sample.prepare_csi500_sample_one_shot_intent(
        target,
        calendar_evidence=evidence,
    )
    prepared = sample.load_sample_target(target.directory)
    second = sample.prepare_csi500_sample_one_shot_intent(
        prepared,
        calendar_evidence=evidence,
    )

    assert first == second
    assert prepared.phase == "intent_prepared"
    assert prepared.state["one_shot_intent_sha256"] == first.intent_sha256
    assert prepared.state["one_shot_scope_status"] == "CANDIDATE_NOT_LOCKED"

    tampered_state = dict(prepared.state)
    original_digest = str(tampered_state["one_shot_intent_sha256"])
    tampered_state["one_shot_intent_sha256"] = (
        "0" * 64 if original_digest != "0" * 64 else "1" * 64
    )
    manifest_path = prepared.directory / sample.SAMPLE_MANIFEST_FILENAME
    manifest_path.write_text(
        json.dumps(tampered_state, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    with pytest.raises(sample.Csi500SampleCompositionError) as error:
        sample.prepare_csi500_sample_one_shot_intent(
            prepared,
            calendar_evidence=evidence,
        )

    assert error.value.code == "sample_intent_manifest_mismatch"
    assert sample.load_sample_target(prepared.directory).phase == "intent_prepared"
