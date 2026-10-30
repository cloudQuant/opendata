"""Offline checks for signed, fenced Sina LRB capture-recovery receipts."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import pytest

import opendata.pipeline.run_service as run_service_module
import opendata.pipeline.sina_lrb_raw_capture as raw_capture_module
from opendata.data.providers.sina.lrb_statement_process import SinaLrbQuery
from opendata.pipeline.raw_evidence_reader import RawResponseArtifactReader
from opendata.pipeline.reviewed_worker import ProcessFetchHandler
from opendata.pipeline.run_identity import RunRequest, TaskDefinition, build_run_identity
from opendata.pipeline.run_ledger import RunRecord
from opendata.pipeline.run_service import RunExecutionContext
from opendata.pipeline.sina_lrb_process_job import (
    SinaLrbProcessCapture,
    SinaLrbRawCaptureNormalizer,
    normalize_sina_lrb_capture,
)
from opendata.pipeline.sina_lrb_raw_capture import (
    SINA_LRB_RAW_RESPONSE_SOURCE,
    CaptureReceiptSigner,
    SinaLrbCaptureRecoveryJournal,
    SinaLrbRawAuthorization,
    SinaLrbRawCaptureError,
    SinaLrbRawCaptureHandler,
    sina_lrb_request_summary_sha256,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
_RETRIEVED_AT = datetime(2020, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
_BODY = b'{"synthetic":"lrb recovery"}'
_SIGNING_KEY = b"offline-test-only-key-for-receipts"


class _StableTestSigner:
    """Deterministic test signer whose key stays in this test process."""

    key_id = "offline-test-key-v1"

    def sign(self, payload: bytes) -> bytes:
        return hmac.digest(_SIGNING_KEY, payload, "sha256")

    def verify(self, key_id: str, payload: bytes, signature: bytes) -> bool:
        return key_id == self.key_id and hmac.compare_digest(self.sign(payload), signature)


class _InvalidTestSigner:
    """Return a signature that its own verification operation rejects."""

    key_id = "offline-invalid-test-key-v1"

    def sign(self, payload: bytes) -> bytes:
        del payload
        return b"not-a-valid-signature"

    def verify(self, key_id: str, payload: bytes, signature: bytes) -> bool:
        del key_id, payload, signature
        return 1  # type: ignore[return-value]


class _FixedProcessHandler(ProcessFetchHandler):
    """In-memory process-handler stand-in with no child or network access."""

    def __init__(self, body: bytes = _BODY) -> None:
        self.calls = 0
        self._heartbeat_interval_seconds = 0.05
        self._body = body

    @property
    def heartbeat_interval_seconds(self) -> float:
        return self._heartbeat_interval_seconds

    def __call__(
        self,
        request: RunRequest,
        context: RunExecutionContext,
    ) -> SinaLrbProcessCapture:
        del request, context
        self.calls += 1
        return SinaLrbProcessCapture(
            query=SinaLrbQuery("600519.SH"),
            status_code=200,
            retrieved_at_utc=_RETRIEVED_AT,
            body=self._body,
        )


def _task() -> TaskDefinition:
    return TaskDefinition(
        job_id="sina-lrb-capture",
        version="process-v1",
        dataset_id="sina_lrb_report_observations",
        provider="sina",
        model="financial_statement",
        enabled=False,
        schedule=None,
    )


def _request(idempotency_key: str = "offline-recovery-request-1") -> RunRequest:
    return RunRequest(
        run_type="manual",
        query={},
        partition={"instrument_id": "600519.SH"},
        shard=None,
        idempotency_key=idempotency_key,
    )


def _context(
    fencing_token: int,
    *,
    checkpoint: Callable[[], None] | None = None,
    with_snapshot: bool = False,
) -> RunExecutionContext:
    identity = build_run_identity(_task(), _request())
    snapshot_identity = (
        run_service_module._VerifiedRunSnapshot(
            run_key=identity.idempotency_key,
            payload_hash=identity.payload_hash,
            canonical_json=identity.canonical_json,
            _issuer=run_service_module._RUN_SNAPSHOT_IDENTITY_ISSUER,
        )
        if with_snapshot
        else None
    )
    return RunExecutionContext(
        run_key=identity.idempotency_key,
        worker=f"offline-worker-{fencing_token}",
        fencing_token=fencing_token,
        _checkpoint=checkpoint if checkpoint is not None else (lambda: None),
        _snapshot_identity=snapshot_identity,
    )


def _handler(
    root: Path,
    delegate: _FixedProcessHandler,
    *,
    reuse_prior_captures: bool = True,
    signed: bool = True,
    receipt_signer: CaptureReceiptSigner | None = None,
) -> SinaLrbRawCaptureHandler:
    return SinaLrbRawCaptureHandler(
        delegate,
        artifact_root=root,
        authorization=SinaLrbRawAuthorization(
            source_access_ref="source-auth:offline-test",
            raw_retention_ref="raw-retention:offline-test",
            source_access_authorized=True,
            raw_retention_authorized=True,
        ),
        retention_class="restricted",
        source_history_limit="unknown",
        task=_task(),
        receipt_signer=(
            receipt_signer
            if receipt_signer is not None
            else (_StableTestSigner() if signed else None)
        ),
        reuse_prior_captures=reuse_prior_captures,
    )


def test_duplicate_delivery_and_new_worker_reuse_verified_capture(tmp_path: Path) -> None:
    """A repeated input reuses one verified response under a fresh fence token."""
    root = tmp_path / "private-raw"
    first_delegate = _FixedProcessHandler()
    first_handler = _handler(root, first_delegate)
    first = first_handler(_request(), _context(1))
    duplicate = first_handler(_request(), _context(1))

    restarted_delegate = _FixedProcessHandler()
    restarted_handler = _handler(root, restarted_delegate)
    restarted = restarted_handler(_request(), _context(2))

    assert first.manifest_sha256 == duplicate.manifest_sha256 == restarted.manifest_sha256
    assert restarted.fencing_token == 2
    assert restarted.origin_fencing_token == 1
    assert first_delegate.calls == 1
    assert restarted_delegate.calls == 0

    identity = build_run_identity(_task(), _request())
    journal = SinaLrbCaptureRecoveryJournal(root, _StableTestSigner())
    journal.verify_capture_receipt(
        restarted,
        request_payload_sha256=identity.payload_hash,
        fencing_token=2,
    )
    evidence = RawResponseArtifactReader(root).read(
        restarted.manifest_sha256,
        expected_source=SINA_LRB_RAW_RESPONSE_SOURCE,
        expected_request_summary_sha256=restarted.request_summary_sha256,
        captured_at_not_after=datetime.now(timezone.utc),
    )
    assert evidence.body == _BODY
    assert {event.event_type for event in journal.events_for_run(restarted.run_key)} == {
        "attempt_started",
        "capture_archived",
        "capture_reused",
    }


def test_receipt_reuse_requires_exact_request_payload_and_fence(tmp_path: Path) -> None:
    """A valid signature cannot move a capture to a different input or future fence."""
    root = tmp_path / "private-raw"
    delegate = _FixedProcessHandler()
    handler = _handler(root, delegate)
    capture = handler(_request(), _context(1))
    identity = build_run_identity(_task(), _request())
    journal = SinaLrbCaptureRecoveryJournal(root, _StableTestSigner())

    assert (
        journal.reusable_capture(
            run_key=capture.run_key,
            request_payload_sha256="0" * 64,
            request_summary_sha256=capture.request_summary_sha256,
            instrument_id=capture.instrument_id,
            current_fencing_token=2,
        )
        is None
    )
    with pytest.raises(SinaLrbRawCaptureError) as error:
        journal.reusable_capture(
            run_key=capture.run_key,
            request_payload_sha256=identity.payload_hash,
            request_summary_sha256=capture.request_summary_sha256,
            instrument_id=capture.instrument_id,
            current_fencing_token=0,
        )
    assert error.value.code == "recovery_invalid"


def test_request_identity_keeps_prefix_shaped_user_tokens_distinct_from_snapshots() -> None:
    """Only a matching canonical snapshot can authorize its synthetic request key."""
    task = _task()
    original_token = f"sha256-{'a' * 64}"
    original_request = _request(original_token)
    identity = build_run_identity(task, original_request)
    ordinary = raw_capture_module._sina_lrb_request_identity(
        task,
        original_request,
        expected_run_key=identity.idempotency_key,
        snapshot_json=None,
    )

    synthetic_request = original_request.model_copy(
        update={
            "idempotency_key": (
                "sha256-" + hashlib.sha256(original_token.encode("utf-8")).hexdigest()
            )
        }
    )
    restored = raw_capture_module._sina_lrb_request_identity(
        task,
        synthetic_request,
        expected_run_key=identity.idempotency_key,
        snapshot_json=identity.canonical_json,
    )

    assert ordinary == (identity.idempotency_key, identity.payload_hash)
    assert restored == (identity.idempotency_key, identity.payload_hash)
    with pytest.raises(SinaLrbRawCaptureError):
        raw_capture_module._sina_lrb_request_identity(
            task,
            synthetic_request,
            expected_run_key=identity.idempotency_key,
            snapshot_json=None,
        )


def test_snapshot_identity_rejects_cross_run_task_and_tampered_request() -> None:
    """Snapshot provenance cannot be relabeled across keys, task versions, or inputs."""
    task = _task()
    request = _request("identity-origin")
    identity = build_run_identity(task, request)
    synthetic_request = request.model_copy(
        update={
            "idempotency_key": (
                "sha256-" + hashlib.sha256(request.idempotency_key.encode("utf-8")).hexdigest()
            )
        }
    )

    other_request = _request("different-logical-run")
    other_identity = build_run_identity(task, other_request)
    other_synthetic_request = other_request.model_copy(
        update={
            "idempotency_key": (
                "sha256-"
                + hashlib.sha256(other_request.idempotency_key.encode("utf-8")).hexdigest()
            )
        }
    )
    with pytest.raises(SinaLrbRawCaptureError):
        raw_capture_module._sina_lrb_request_identity(
            task,
            other_synthetic_request,
            expected_run_key=other_identity.idempotency_key,
            snapshot_json=identity.canonical_json,
        )

    with pytest.raises(SinaLrbRawCaptureError):
        raw_capture_module._sina_lrb_request_identity(
            task.model_copy(update={"version": "process-v2"}),
            synthetic_request,
            expected_run_key=identity.idempotency_key,
            snapshot_json=identity.canonical_json,
        )

    altered_snapshot = json.loads(identity.canonical_json)
    altered_snapshot["run"]["business_request"]["partition"]["instrument_id"] = "600001.SH"
    tampered_json = json.dumps(altered_snapshot, sort_keys=True, separators=(",", ":"))
    with pytest.raises(SinaLrbRawCaptureError):
        raw_capture_module._sina_lrb_request_identity(
            task,
            synthetic_request,
            expected_run_key=identity.idempotency_key,
            snapshot_json=tampered_json,
        )


def test_lost_lease_stops_before_attempt_receipt_or_provider_call(tmp_path: Path) -> None:
    """A failed active-lease checkpoint cannot start provider or journal work."""
    root = tmp_path / "private-raw"
    delegate = _FixedProcessHandler()
    handler = _handler(root, delegate)

    def lost_lease() -> None:
        raise RuntimeError("lease is no longer active")

    with pytest.raises(RuntimeError, match="lease is no longer active"):
        handler(_request(), _context(1, checkpoint=lost_lease))

    identity = build_run_identity(_task(), _request())
    journal = SinaLrbCaptureRecoveryJournal(root, _StableTestSigner())
    assert delegate.calls == 0
    assert journal.events_for_run(identity.idempotency_key) == ()


def test_invalid_signer_is_verified_before_provider_or_raw_artifact(tmp_path: Path) -> None:
    """An unverified attempt receipt fails before the provider or raw writer runs."""
    root = tmp_path / "private-raw"
    delegate = _FixedProcessHandler()
    handler = _handler(
        root,
        delegate,
        receipt_signer=_InvalidTestSigner(),
    )

    with pytest.raises(SinaLrbRawCaptureError) as error:
        handler(_request(), _context(1))

    assert error.value.code == "recovery_signature_invalid"
    assert delegate.calls == 0
    identity = build_run_identity(_task(), _request())
    journal = SinaLrbCaptureRecoveryJournal(root, _InvalidTestSigner())
    assert journal.events_for_run(identity.idempotency_key) == ()
    assert not (root / "manifests").exists()
    assert not (root / "objects").exists()


def test_old_fence_capture_is_rejected_by_helper_and_default_normalizer(
    tmp_path: Path,
) -> None:
    """A no-signer normalizer still requires the exact task and active fence."""
    root = tmp_path / "private-raw"
    archived = _handler(root, _FixedProcessHandler())(_request(), _context(1))
    stale_context = _context(2, with_snapshot=True)
    assert stale_context._verified_snapshot_json() is not None

    with pytest.raises(ValueError, match="sina_lrb_capture_invalid"):
        normalize_sina_lrb_capture(
            archived,
            _request(),
            stale_context,
            artifact_root=root,
            task=_task(),
        )

    with pytest.raises(ValueError, match="sina_lrb_capture_invalid"):
        SinaLrbRawCaptureNormalizer(root, _task())(archived, _request(), stale_context)

    unknown_fence = replace(archived, fencing_token=None)
    with pytest.raises(ValueError, match="sina_lrb_capture_invalid"):
        normalize_sina_lrb_capture(
            unknown_fence,
            _request(),
            _context(1),
            artifact_root=root,
            task=_task(),
        )
    with pytest.raises(ValueError, match="sina_lrb_capture_invalid"):
        SinaLrbRawCaptureNormalizer(root, _task())(
            unknown_fence,
            _request(),
            _context(1),
        )

    bool_fence_context = _context(True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="sina_lrb_capture_invalid"):
        normalize_sina_lrb_capture(
            archived,
            _request(),
            bool_fence_context,
            artifact_root=root,
            task=_task(),
        )
    with pytest.raises(ValueError, match="sina_lrb_capture_invalid"):
        SinaLrbRawCaptureNormalizer(root, _task())(
            archived,
            _request(),
            bool_fence_context,
        )


def test_normalizer_fails_closed_without_task_snapshot(tmp_path: Path) -> None:
    """A fence alone cannot bind an archived response to canonical run inputs."""
    root = tmp_path / "private-raw"
    archived = _handler(root, _FixedProcessHandler())(_request(), _context(1))
    current_context = _context(1)

    with pytest.raises(ValueError, match="sina_lrb_capture_invalid"):
        normalize_sina_lrb_capture(
            archived,
            _request(),
            current_context,
            artifact_root=root,
            task=None,  # type: ignore[arg-type]
        )

    with pytest.raises(ValueError, match="sina_lrb_capture_invalid"):
        SinaLrbRawCaptureNormalizer(root, None)(  # type: ignore[arg-type]
            archived,
            _request(),
            current_context,
        )


def test_restart_removes_only_incomplete_private_journal_temporary_files(
    tmp_path: Path,
) -> None:
    """A crash before final-link publication leaves no adopted receipt."""
    root = tmp_path / "private-raw"
    identity = build_run_identity(_task(), _request())
    journal = SinaLrbCaptureRecoveryJournal(root, _StableTestSigner())
    journal.record_attempt_started(
        run_key=identity.idempotency_key,
        request_payload_sha256=identity.payload_hash,
        request_summary_sha256=sina_lrb_request_summary_sha256("600519.SH"),
        instrument_id="600519.SH",
        fencing_token=1,
    )
    event_directory = root / "lrbjournal" / "runs" / identity.idempotency_key / "events"
    incomplete = event_directory / ".tmp-0123456789abcdef0123456789abcdef"
    incomplete.write_bytes(b"incomplete signed envelope")
    incomplete.chmod(0o600)

    events = journal.events_for_run(identity.idempotency_key)

    assert [event.event_type for event in events] == ["attempt_started"]
    assert not incomplete.exists()


def test_conflicting_capture_receipts_fail_closed(tmp_path: Path) -> None:
    """Two different captures under the latest token cannot be auto-selected."""
    root = tmp_path / "private-raw"
    identity = build_run_identity(_task(), _request())
    digest = sina_lrb_request_summary_sha256("600519.SH")
    first = _handler(
        root,
        _FixedProcessHandler(_BODY),
        reuse_prior_captures=False,
    )(_request(), _context(1))
    second = _handler(
        root,
        _FixedProcessHandler(b'{"synthetic":"different"}'),
        reuse_prior_captures=False,
    )(_request(), _context(1))
    assert first.manifest_sha256 != second.manifest_sha256

    journal = SinaLrbCaptureRecoveryJournal(root, _StableTestSigner())
    with pytest.raises(SinaLrbRawCaptureError) as error:
        journal.reusable_capture(
            run_key=identity.idempotency_key,
            request_payload_sha256=identity.payload_hash,
            request_summary_sha256=digest,
            instrument_id="600519.SH",
            current_fencing_token=1,
        )

    assert error.value.code == "recovery_ambiguous"


def test_journal_revalidates_manifest_and_canonical_request_before_signing(
    tmp_path: Path,
) -> None:
    """A valid attempt cannot authorize a forged DTO or a different request."""
    root = tmp_path / "private-raw"
    archived = _handler(
        root,
        _FixedProcessHandler(),
        reuse_prior_captures=False,
        signed=False,
    )(_request(), _context(1))
    identity = build_run_identity(_task(), _request())
    digest = sina_lrb_request_summary_sha256("600519.SH")
    journal = SinaLrbCaptureRecoveryJournal(root, _StableTestSigner())
    journal.record_attempt_started(
        run_key=identity.idempotency_key,
        request_payload_sha256=identity.payload_hash,
        request_summary_sha256=digest,
        instrument_id="600519.SH",
        fencing_token=1,
    )
    with pytest.raises(SinaLrbRawCaptureError) as error:
        journal.record_capture(
            replace(archived, manifest_sha256="0" * 64),
            task=_task(),
            request=_request(),
            request_payload_sha256=identity.payload_hash,
            fencing_token=1,
        )
    assert error.value.code == "recovery_invalid"

    wrong_request = _request().model_copy(
        update={"idempotency_key": "different-request"},
    )
    with pytest.raises(SinaLrbRawCaptureError) as error:
        journal.record_capture(
            archived,
            task=_task(),
            request=wrong_request,
            request_payload_sha256=identity.payload_hash,
            fencing_token=1,
        )
    assert error.value.code == "invalid_capture"
    assert [event.event_type for event in journal.events_for_run(identity.idempotency_key)] == [
        "attempt_started"
    ]

    malformed_context = replace(_context(1, with_snapshot=True), fencing_token=True)
    with pytest.raises(SinaLrbRawCaptureError) as error:
        journal.record_capture(
            archived,
            task=_task(),
            request=_request(),
            request_payload_sha256=identity.payload_hash,
            fencing_token=1,
            context=malformed_context,
        )
    assert error.value.code == "invalid_capture"


def test_reuse_receipt_requires_same_run_signed_origin(tmp_path: Path) -> None:
    """A valid same-instrument artifact from another run cannot be reused by relabeling it."""
    root = tmp_path / "private-raw"
    origin_request = _request()
    origin_identity = build_run_identity(_task(), origin_request)
    origin_capture = _handler(root, _FixedProcessHandler())(origin_request, _context(1))

    current_request = _request("different-logical-run")
    current_identity = build_run_identity(_task(), current_request)
    current_journal = SinaLrbCaptureRecoveryJournal(root, _StableTestSigner())
    current_journal.record_attempt_started(
        run_key=current_identity.idempotency_key,
        request_payload_sha256=current_identity.payload_hash,
        request_summary_sha256=origin_capture.request_summary_sha256,
        instrument_id=origin_capture.instrument_id,
        fencing_token=2,
    )
    forged = replace(
        origin_capture,
        run_key=current_identity.idempotency_key,
        request_payload_sha256=current_identity.payload_hash,
        fencing_token=2,
        origin_fencing_token=1,
    )
    assert origin_identity.idempotency_key != current_identity.idempotency_key

    with pytest.raises(SinaLrbRawCaptureError) as error:
        current_journal.record_capture(
            forged,
            task=_task(),
            request=current_request,
            request_payload_sha256=current_identity.payload_hash,
            fencing_token=2,
            origin_fencing_token=1,
        )
    assert error.value.code == "recovery_invalid"

    forged_payload = raw_capture_module._receipt_payload(
        event_type="capture_reused",
        run_key=current_identity.idempotency_key,
        request_payload_sha256=current_identity.payload_hash,
        request_summary_sha256=forged.request_summary_sha256,
        instrument_id=forged.instrument_id,
        fencing_token=2,
        created_at_utc=_NOW,
        key_id=_StableTestSigner.key_id,
        manifest_sha256=forged.manifest_sha256,
        response_sha256=forged.response_sha256,
        status_code=forged.status_code,
        retrieved_at_utc=forged.retrieved_at_utc,
        captured_at_utc=forged.captured_at_utc,
        source_access_ref=forged.source_access_ref,
        raw_retention_ref=forged.raw_retention_ref,
        origin_fencing_token=1,
    )
    with pytest.raises(SinaLrbRawCaptureError) as error:
        current_journal._append_signed_event(forged_payload)
    assert error.value.code == "recovery_invalid"

    canonical_payload = raw_capture_module._canonical_json_bytes(forged_payload)
    forged_envelope = {
        "payload": forged_payload,
        "signature": base64.urlsafe_b64encode(_StableTestSigner().sign(canonical_payload))
        .decode("ascii")
        .rstrip("="),
    }
    encoded = raw_capture_module._canonical_json_bytes(forged_envelope)
    digest = hashlib.sha256(encoded).hexdigest()
    event_path = (
        root
        / "lrbjournal"
        / "runs"
        / current_identity.idempotency_key
        / "events"
        / f"{digest}.json"
    )
    event_path.write_bytes(encoded)
    event_path.chmod(0o600)
    with pytest.raises(SinaLrbRawCaptureError) as error:
        current_journal.events_for_run(current_identity.idempotency_key)
    assert error.value.code == "recovery_invalid"
    with pytest.raises(SinaLrbRawCaptureError) as error:
        current_journal.verify_capture_receipt(
            forged,
            request_payload_sha256=current_identity.payload_hash,
            fencing_token=2,
        )
    assert error.value.code == "recovery_invalid"


def test_journal_event_limit_is_checked_under_lock_and_duplicate_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repeated event stays one record, while a new over-limit append fails closed."""
    root = tmp_path / "private-raw"
    identity = build_run_identity(_task(), _request())
    journal = SinaLrbCaptureRecoveryJournal(root, _StableTestSigner())
    payload = raw_capture_module._receipt_payload(
        event_type="attempt_started",
        run_key=identity.idempotency_key,
        request_payload_sha256=identity.payload_hash,
        request_summary_sha256=sina_lrb_request_summary_sha256("600519.SH"),
        instrument_id="600519.SH",
        fencing_token=1,
        created_at_utc=_NOW,
        key_id=_StableTestSigner.key_id,
    )
    journal._append_signed_event(payload)
    journal._append_signed_event(payload)
    assert len(journal.events_for_run(identity.idempotency_key)) == 1

    monkeypatch.setattr(raw_capture_module, "_MAX_JOURNAL_EVENTS_PER_RUN", 1)
    over_limit_payload = raw_capture_module._receipt_payload(
        event_type="attempt_started",
        run_key=identity.idempotency_key,
        request_payload_sha256=identity.payload_hash,
        request_summary_sha256=sina_lrb_request_summary_sha256("600519.SH"),
        instrument_id="600519.SH",
        fencing_token=1,
        created_at_utc=_NOW + timedelta(seconds=1),
        key_id=_StableTestSigner.key_id,
    )
    with pytest.raises(SinaLrbRawCaptureError) as error:
        journal._append_signed_event(over_limit_payload)
    assert error.value.code == "recovery_invalid"
    assert len(journal.events_for_run(identity.idempotency_key)) == 1


def test_tampered_receipt_signature_is_rejected_even_with_new_content_address(
    tmp_path: Path,
) -> None:
    """Rehashing an edited event does not forge the externally signed payload."""
    root = tmp_path / "private-raw"
    identity = build_run_identity(_task(), _request())
    journal = SinaLrbCaptureRecoveryJournal(root, _StableTestSigner())
    journal.record_attempt_started(
        run_key=identity.idempotency_key,
        request_payload_sha256=identity.payload_hash,
        request_summary_sha256=sina_lrb_request_summary_sha256("600519.SH"),
        instrument_id="600519.SH",
        fencing_token=1,
    )
    event_directory = root / "lrbjournal" / "runs" / identity.idempotency_key / "events"
    event_path = next(event_directory.glob("*.json"))
    envelope = json.loads(event_path.read_text(encoding="utf-8"))
    envelope["payload"]["fencing_token"] = 2
    tampered = json.dumps(
        envelope,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    forged_path = event_directory / f"{hashlib.sha256(tampered).hexdigest()}.json"
    forged_path.write_bytes(tampered)
    forged_path.chmod(0o600)

    with pytest.raises(SinaLrbRawCaptureError) as error:
        journal.events_for_run(identity.idempotency_key)

    assert error.value.code == "recovery_signature_invalid"


def test_audit_marks_prepublication_capture_as_candidate_and_unlinked_start(
    tmp_path: Path,
) -> None:
    """The ledger state and signed journal expose crash windows without guessing."""
    root = tmp_path / "private-raw"
    identity = build_run_identity(_task(), _request())
    digest = sina_lrb_request_summary_sha256("600519.SH")
    journal = SinaLrbCaptureRecoveryJournal(root, _StableTestSigner())
    journal.record_attempt_started(
        run_key=identity.idempotency_key,
        request_payload_sha256=identity.payload_hash,
        request_summary_sha256=digest,
        instrument_id="600519.SH",
        fencing_token=1,
    )
    record = RunRecord(
        run_key=identity.idempotency_key,
        job_snapshot_json=identity.canonical_json,
        status="failed",
        worker=None,
        lease_id=None,
        fencing_token=1,
        lease_expires_at=None,
        created_at=_NOW,
        updated_at=_NOW,
        started_at=_NOW,
        heartbeat_at=_NOW,
        finished_at=_NOW,
        error_text="synthetic_failure",
    )

    report = journal.audit_run(record)

    assert report.disposition == "unlinked_artifact_possible"
    assert report.capture_manifest_sha256s == ()
    assert report.attempt_fencing_tokens == (1,)


def test_audit_rejects_receipts_that_do_not_match_ledger_snapshot(tmp_path: Path) -> None:
    """A signed run key alone cannot substitute for the ledger payload hash."""
    root = tmp_path / "private-raw"
    delegate = _FixedProcessHandler()
    capture = _handler(root, delegate)(_request(), _context(1))
    record = RunRecord(
        run_key=capture.run_key,
        job_snapshot_json="{}",
        status="failed",
        worker=None,
        lease_id=None,
        fencing_token=1,
        lease_expires_at=None,
        created_at=_NOW,
        updated_at=_NOW,
        started_at=_NOW,
        heartbeat_at=_NOW,
        finished_at=_NOW,
        error_text="synthetic_failure",
    )
    journal = SinaLrbCaptureRecoveryJournal(root, _StableTestSigner())

    with pytest.raises(SinaLrbRawCaptureError) as error:
        journal.audit_run(record)

    assert error.value.code == "recovery_invalid"
