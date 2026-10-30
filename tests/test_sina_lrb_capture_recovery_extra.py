"""Additional offline recovery-boundary cases for Sina LRB raw captures."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import pytest

import opendata.pipeline.run_service as run_service_module
import opendata.pipeline.sina_lrb_raw_capture as raw_capture
from opendata.data.providers.sina.lrb_statement_process import SinaLrbQuery
from opendata.pipeline.reviewed_worker import ProcessFetchHandler
from opendata.pipeline.run_identity import (
    RunIdentity,
    RunRequest,
    TaskDefinition,
    build_run_identity,
)
from opendata.pipeline.run_ledger import RunRecord
from opendata.pipeline.run_service import RunExecutionContext
from opendata.pipeline.sina_lrb_process_job import SinaLrbProcessCapture
from opendata.pipeline.sina_lrb_raw_capture import (
    SinaLrbArchivedCapture,
    SinaLrbCaptureRecoveryJournal,
    SinaLrbRawAuthorization,
    SinaLrbRawCaptureError,
    SinaLrbRawCaptureHandler,
    sina_lrb_request_summary_sha256,
)

if TYPE_CHECKING:
    from pathlib import Path

_NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
_RETRIEVED = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
_BODY = b'{"synthetic":"recovery-boundary"}'
_SIGNING_KEY = b"offline-only-sina-lrb-receipt-key"


class _Signer:
    """Stable HMAC signer used only by isolated offline tests."""

    key_id = "offline-recovery-key-v1"

    def sign(self, payload: bytes) -> bytes:
        return hmac.digest(_SIGNING_KEY, payload, "sha256")

    def verify(self, key_id: str, payload: bytes, signature: bytes) -> bool:
        return key_id == self.key_id and hmac.compare_digest(self.sign(payload), signature)


class _RaisingSignSigner(_Signer):
    def sign(self, payload: bytes) -> bytes:
        del payload
        raise RuntimeError("signing service unavailable")


class _RaisingVerifySigner(_Signer):
    def verify(self, key_id: str, payload: bytes, signature: bytes) -> bool:
        del key_id, payload, signature
        raise RuntimeError("verification service unavailable")


class _TruthyVerifySigner(_Signer):
    def verify(self, key_id: str, payload: bytes, signature: bytes) -> bool:
        del key_id, payload, signature
        return 1  # type: ignore[return-value]


class _TruthyVerifySignerWithBadId(_Signer):
    key_id = "Bearer offline credential"


class _FixedHandler(ProcessFetchHandler):
    """In-memory reviewed handler; these tests never start a child process."""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def heartbeat_interval_seconds(self) -> float:
        return 0.05

    def __call__(self, request: RunRequest, context: RunExecutionContext) -> object:
        del request, context
        self.calls += 1
        return SinaLrbProcessCapture(
            query=SinaLrbQuery("600519.SH"),
            status_code=200,
            retrieved_at_utc=_RETRIEVED,
            body=_BODY,
        )


def _task() -> TaskDefinition:
    return TaskDefinition(
        job_id="sina-lrb-extra-tests",
        version="offline-v1",
        dataset_id="sina_lrb_report_observations",
        provider="sina",
        model="financial_statement",
        enabled=False,
        schedule=None,
    )


def _request(token: str = "offline-extra-recovery-1") -> RunRequest:
    return RunRequest(
        run_type="manual",
        query={},
        partition={"instrument_id": "600519.SH"},
        shard=None,
        idempotency_key=token,
    )


def _identity(request: RunRequest | None = None) -> RunIdentity:
    return build_run_identity(_task(), request or _request())


def _context(fencing_token: int, request: RunRequest | None = None) -> RunExecutionContext:
    identity = _identity(request)
    return RunExecutionContext(
        run_key=identity.idempotency_key,
        worker=f"offline-worker-{fencing_token}",
        fencing_token=fencing_token,
        _checkpoint=lambda: None,
    )


def _handler(
    root: Path,
    *,
    signer: _Signer | None = None,
    reuse_prior_captures: bool = False,
) -> tuple[SinaLrbRawCaptureHandler, _FixedHandler]:
    delegate = _FixedHandler()
    handler = SinaLrbRawCaptureHandler(
        delegate,
        artifact_root=root,
        authorization=SinaLrbRawAuthorization(
            source_access_ref="source-auth:offline-extra",
            raw_retention_ref="raw-retention:offline-extra",
            source_access_authorized=True,
            raw_retention_authorized=True,
        ),
        retention_class="restricted",
        source_history_limit="unknown",
        task=_task() if signer is not None else None,
        receipt_signer=signer,
        reuse_prior_captures=reuse_prior_captures,
    )
    return handler, delegate


def _attempt_payload(
    *, fencing_token: int = 1, request: RunRequest | None = None
) -> dict[str, object]:
    identity = _identity(request)
    return raw_capture._receipt_payload(
        event_type="attempt_started",
        run_key=identity.idempotency_key,
        request_payload_sha256=identity.payload_hash,
        request_summary_sha256=sina_lrb_request_summary_sha256("600519.SH"),
        instrument_id="600519.SH",
        fencing_token=fencing_token,
        created_at_utc=_NOW,
        key_id=_Signer.key_id,
    )


def _capture_payload(
    *,
    fencing_token: int = 1,
    origin_fencing_token: int | None = None,
    manifest_sha256: str = "a" * 64,
    request: RunRequest | None = None,
) -> dict[str, object]:
    identity = _identity(request)
    return raw_capture._receipt_payload(
        event_type="capture_reused" if origin_fencing_token is not None else "capture_archived",
        run_key=identity.idempotency_key,
        request_payload_sha256=identity.payload_hash,
        request_summary_sha256=sina_lrb_request_summary_sha256("600519.SH"),
        instrument_id="600519.SH",
        fencing_token=fencing_token,
        created_at_utc=_NOW,
        key_id=_Signer.key_id,
        manifest_sha256=manifest_sha256,
        response_sha256=hashlib.sha256(_BODY).hexdigest(),
        status_code=200,
        retrieved_at_utc=_RETRIEVED,
        captured_at_utc=_NOW,
        source_access_ref="source-auth:offline-extra",
        raw_retention_ref="raw-retention:offline-extra",
        origin_fencing_token=origin_fencing_token,
    )


def _encode_signed(payload: dict[str, object], signer: _Signer | None = None) -> bytes:
    actual_signer = signer or _Signer()
    canonical_payload = raw_capture._canonical_json_bytes(payload)
    signature = actual_signer.sign(canonical_payload)
    envelope = {
        "payload": payload,
        "signature": base64.urlsafe_b64encode(signature).decode("ascii").rstrip("="),
    }
    return raw_capture._canonical_json_bytes(envelope)


def _record(identity: RunIdentity, status: str) -> RunRecord:
    return RunRecord(
        run_key=identity.idempotency_key,
        job_snapshot_json=identity.canonical_json,
        status=status,  # type: ignore[arg-type]
        worker=None,
        lease_id=None,
        fencing_token=1,
        lease_expires_at=None,
        created_at=_NOW,
        updated_at=_NOW,
        started_at=_NOW,
        heartbeat_at=_NOW,
        finished_at=_NOW,
        error_text=None,
    )


@pytest.mark.parametrize(
    ("root", "signer", "error_type", "error_code"),
    [
        (None, _Signer(), TypeError, None),
        ("relative-artifacts", _Signer(), ValueError, None),
        ("/", _Signer(), ValueError, None),
        ("/tmp/lrb-journal-root", object(), TypeError, None),
        (
            "/tmp/lrb-journal-root",
            _TruthyVerifySignerWithBadId(),
            SinaLrbRawCaptureError,
            "recovery_signing_failed",
        ),
    ],
)
def test_journal_constructor_rejects_unsafe_root_and_signer_contract(
    root: object,
    signer: object,
    error_type: type[Exception],
    error_code: str | None,
) -> None:
    """Construction validates the root and signer before any journal I/O."""
    with pytest.raises(error_type) as error:
        SinaLrbCaptureRecoveryJournal(root, signer)  # type: ignore[arg-type]
    if error_code is not None:
        assert isinstance(error.value, SinaLrbRawCaptureError)
        assert error.value.code == error_code


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"journal_version": True}, "recovery_invalid"),
        ({"event_type": "unknown"}, "recovery_invalid"),
        ({"fencing_token": True}, "recovery_invalid"),
        ({"created_at_utc": "2026-10-30T12:00:00+00:00"}, "recovery_invalid"),
        ({"extra": "not-in-the-schema"}, "recovery_invalid"),
    ],
)
def test_receipt_parser_rejects_version_event_type_and_strict_field_drift(
    changes: dict[str, object], code: str
) -> None:
    """Persisted receipt parsing rejects schema drift and bool-as-integer values."""
    payload = dict(_attempt_payload())
    payload.update(changes)
    with pytest.raises(SinaLrbRawCaptureError) as error:
        raw_capture._receipt_from_payload(payload)
    assert error.value.code == code


def test_capture_receipt_parser_rejects_malformed_manifest_digest() -> None:
    """A structurally complete capture is rejected when its manifest hash is malformed."""
    payload = _capture_payload()
    payload["manifest_sha256"] = "not-a-digest"
    with pytest.raises(SinaLrbRawCaptureError) as error:
        raw_capture._receipt_from_payload(payload)
    assert error.value.code == "recovery_invalid"


@pytest.mark.parametrize(
    "payload",
    [
        {"run_key": 123},
        {"request_payload_sha256": True},
        {"request_summary_sha256": None},
        {"instrument_id": "600519"},
        {"fencing_token": False},
    ],
)
def test_receipt_identity_validation_rejects_wrong_types_and_noncanonical_symbols(
    payload: dict[str, object],
) -> None:
    """Identity validation treats symbols, hashes and fencing as strict input."""
    identity = _identity()
    values: dict[str, object] = {
        "run_key": identity.idempotency_key,
        "request_payload_sha256": identity.payload_hash,
        "request_summary_sha256": sina_lrb_request_summary_sha256("600519.SH"),
        "instrument_id": "600519.SH",
        "fencing_token": 1,
    }
    values.update(payload)
    with pytest.raises(SinaLrbRawCaptureError) as error:
        raw_capture._validate_receipt_identity(
            values["run_key"],
            values["request_payload_sha256"],
            values["request_summary_sha256"],
            values["instrument_id"],
            values["fencing_token"],
        )
    assert error.value.code == "recovery_invalid"


@pytest.mark.parametrize(
    ("event_type", "capture_values"),
    [
        (
            "attempt_started",
            {"manifest_sha256": "a" * 64},
        ),
        (
            "capture_archived",
            {"captured_at_utc": _RETRIEVED - timedelta(seconds=1)},
        ),
        (
            "capture_archived",
            {"origin_fencing_token": 1},
        ),
        (
            "capture_reused",
            {"origin_fencing_token": 2},
        ),
        (
            "capture_reused",
            {"origin_fencing_token": 0},
        ),
    ],
)
def test_payload_builder_rejects_incomplete_capture_and_impossible_origin_links(
    event_type: str, capture_values: dict[str, object]
) -> None:
    """Receipt serialization requires complete capture facts and a prior origin."""
    identity = _identity()
    defaults: dict[str, object] = {
        "event_type": event_type,
        "run_key": identity.idempotency_key,
        "request_payload_sha256": identity.payload_hash,
        "request_summary_sha256": sina_lrb_request_summary_sha256("600519.SH"),
        "instrument_id": "600519.SH",
        "fencing_token": 2 if event_type == "capture_reused" else 1,
        "created_at_utc": _NOW,
        "key_id": _Signer.key_id,
    }
    if event_type != "attempt_started":
        defaults.update(
            {
                "manifest_sha256": "a" * 64,
                "response_sha256": hashlib.sha256(_BODY).hexdigest(),
                "status_code": 200,
                "retrieved_at_utc": _RETRIEVED,
                "captured_at_utc": _NOW,
                "source_access_ref": "source-auth:offline-extra",
                "raw_retention_ref": "raw-retention:offline-extra",
            }
        )
    defaults.update(capture_values)
    with pytest.raises(SinaLrbRawCaptureError) as error:
        raw_capture._receipt_payload(**defaults)  # type: ignore[arg-type]
    assert error.value.code == "recovery_invalid"


@pytest.mark.parametrize(
    ("encoded", "signer", "expected_code"),
    [
        (b"\xff", _Signer(), "recovery_invalid"),
        (b'{"payload":{},"payload":{},"signature":"abc"}', _Signer(), "recovery_invalid"),
    ],
)
def test_signed_event_decoder_rejects_invalid_utf8_duplicates_and_noncanonical_json(
    encoded: bytes, signer: _Signer, expected_code: str
) -> None:
    """The journal reader accepts only unique-key canonical UTF-8 envelopes."""
    with pytest.raises(SinaLrbRawCaptureError) as error:
        raw_capture._decode_signed_event(encoded, signer)
    assert error.value.code == expected_code


def test_signed_event_decoder_rejects_noncanonical_valid_envelope() -> None:
    """A complete valid signed envelope with whitespace fails only canonical encoding."""
    canonical = _encode_signed(_attempt_payload())
    envelope = json.loads(canonical)
    encoded = json.dumps(envelope, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
    assert raw_capture._canonical_json_bytes(envelope) != encoded

    with pytest.raises(SinaLrbRawCaptureError) as error:
        raw_capture._decode_signed_event(encoded, _Signer())
    assert error.value.code == "recovery_invalid"


@pytest.mark.parametrize(
    ("signature_text", "expected_code"),
    [
        ("=bad", "recovery_invalid"),
        ("A", "recovery_invalid"),
        ("!!!!", "recovery_invalid"),
    ],
)
def test_signed_event_decoder_rejects_bad_signature_encodings(
    signature_text: str, expected_code: str
) -> None:
    """Signature parsing rejects malformed base64url before calling a verifier."""
    envelope = {"payload": _attempt_payload(), "signature": signature_text}
    encoded = raw_capture._canonical_json_bytes(envelope)
    with pytest.raises(SinaLrbRawCaptureError) as error:
        raw_capture._decode_signed_event(encoded, _Signer())
    assert error.value.code == expected_code


def test_signed_event_decoder_requires_literal_true_and_handles_verifier_errors() -> None:
    """A truthy non-bool verifier result and a verifier exception both fail closed."""
    payload = _attempt_payload()
    encoded = _encode_signed(payload)
    with pytest.raises(SinaLrbRawCaptureError) as truthy_error:
        raw_capture._decode_signed_event(encoded, _TruthyVerifySigner())
    assert truthy_error.value.code == "recovery_signature_invalid"

    with pytest.raises(SinaLrbRawCaptureError) as unavailable_error:
        raw_capture._decode_signed_event(encoded, _RaisingVerifySigner())
    assert unavailable_error.value.code == "recovery_signature_invalid"

    decoded = raw_capture._decode_signed_event(encoded, _Signer())
    assert decoded.event_type == "attempt_started"
    assert decoded.fencing_token == 1


def test_append_signing_failure_does_not_publish_any_receipt(tmp_path: Path) -> None:
    """Signing, malformed signature output, and failed self-verification leave no event."""
    identity = _identity()
    payload = _attempt_payload()
    for signer, code in (
        (_RaisingSignSigner(), "recovery_signing_failed"),
        (_EmptySignSigner(), "recovery_signing_failed"),
        (_NonBytesSignSigner(), "recovery_signing_failed"),
        (_TruthyVerifySigner(), "recovery_signature_invalid"),
    ):
        root = tmp_path / f"journal-{code}-{type(signer).__name__}"
        journal = SinaLrbCaptureRecoveryJournal(root, signer)
        with pytest.raises(SinaLrbRawCaptureError) as error:
            journal._append_signed_event(payload)
        assert error.value.code == code
        assert journal.events_for_run(identity.idempotency_key) == ()


def test_private_event_store_is_immutable_and_cleans_failed_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Content-addressed publication is idempotent and removes an interrupted temp."""
    identity = _identity()
    journal = SinaLrbCaptureRecoveryJournal(tmp_path / "private-raw", _Signer())
    journal.record_attempt_started(
        run_key=identity.idempotency_key,
        request_payload_sha256=identity.payload_hash,
        request_summary_sha256=sina_lrb_request_summary_sha256("600519.SH"),
        instrument_id="600519.SH",
        fencing_token=1,
    )
    event_directory = (
        tmp_path / "private-raw" / "lrbjournal" / "runs" / identity.idempotency_key / "events"
    )
    directory_fd = os.open(event_directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        encoded = _encode_signed(_attempt_payload(fencing_token=2))
        digest = hashlib.sha256(encoded).hexdigest()
        name = f"{digest}.json"
        raw_capture._store_private_event(directory_fd, name, encoded, digest)
        raw_capture._store_private_event(directory_fd, name, encoded, digest)
        assert (event_directory / name).read_bytes() == encoded

        with pytest.raises(SinaLrbRawCaptureError) as digest_error:
            raw_capture._store_private_event(directory_fd, name, encoded, "0" * 64)
        assert digest_error.value.code == "recovery_invalid"

        temporary_name = ".tmp-" + "1" * 32
        monkeypatch.setattr(raw_capture.secrets, "token_hex", lambda _size: "1" * 32)

        def fail_write(descriptor: int, content: bytes) -> None:
            del descriptor, content
            raise OSError("simulated interrupted event write")

        monkeypatch.setattr(raw_capture, "_write_all", fail_write)
        failed_encoded = b"another isolated journal event"
        with pytest.raises(OSError, match="simulated interrupted event write"):
            raw_capture._store_private_event(
                directory_fd,
                f"{hashlib.sha256(failed_encoded).hexdigest()}.json",
                failed_encoded,
                hashlib.sha256(failed_encoded).hexdigest(),
            )
        assert not (event_directory / temporary_name).exists()
        raw_capture._unlink_journal_file(directory_fd, temporary_name)
    finally:
        os.close(directory_fd)


class _EmptySignSigner(_Signer):
    def sign(self, payload: bytes) -> bytes:
        del payload
        return b""


class _NonBytesSignSigner(_Signer):
    def sign(self, payload: bytes) -> bytes:
        del payload
        return "not-bytes"  # type: ignore[return-value]


def test_capture_origin_chain_requires_attempt_and_prior_exact_artifact() -> None:
    """A reuse receipt chains only to an exact prior capture and both attempts."""
    archived = raw_capture._receipt_from_payload(_capture_payload(fencing_token=1))
    reused = raw_capture._receipt_from_payload(
        _capture_payload(fencing_token=2, origin_fencing_token=1)
    )
    first_attempt = raw_capture._receipt_from_payload(_attempt_payload(fencing_token=1))
    second_attempt = raw_capture._receipt_from_payload(_attempt_payload(fencing_token=2))

    assert not raw_capture._capture_receipt_origin_chains_are_valid([archived])
    assert raw_capture._capture_receipt_origin_chains_are_valid(
        [first_attempt, archived, second_attempt, reused]
    )

    wrong_origin = replace(reused, origin_fencing_token=99)
    assert not raw_capture._capture_receipt_origin_chains_are_valid(
        [first_attempt, archived, second_attempt, wrong_origin]
    )
    wrong_artifact = replace(reused, response_sha256="b" * 64)
    assert not raw_capture._capture_receipt_origin_chains_are_valid(
        [first_attempt, archived, second_attempt, wrong_artifact]
    )
    origin_on_fresh_capture = replace(archived, origin_fencing_token=1)
    assert not raw_capture._capture_receipt_origin_chains_are_valid(
        [first_attempt, origin_on_fresh_capture]
    )


def test_run_identity_accepts_only_exact_canonical_service_snapshot() -> None:
    """The synthetic request key is trusted only when its task and snapshot agree."""
    original_request = _request()
    identity = _identity(original_request)
    request_digest = hashlib.sha256(original_request.idempotency_key.encode("utf-8")).hexdigest()
    synthetic_request = original_request.model_copy(
        update={"idempotency_key": f"sha256-{request_digest}"},
    )
    snapshot = run_service_module._VerifiedRunSnapshot(
        run_key=identity.idempotency_key,
        payload_hash=identity.payload_hash,
        canonical_json=identity.canonical_json,
        _issuer=run_service_module._RUN_SNAPSHOT_IDENTITY_ISSUER,
    )
    actual_key, actual_payload_hash = raw_capture._sina_lrb_request_identity(
        _task(),
        synthetic_request,
        expected_run_key=identity.idempotency_key,
        snapshot_json=snapshot.canonical_json,
    )
    assert (actual_key, actual_payload_hash) == (identity.idempotency_key, identity.payload_hash)

    duplicate_key_snapshot = snapshot.canonical_json.replace(
        '{"contract_version":1,',
        '{"contract_version":1,"contract_version":1,',
        1,
    )
    noncanonical_snapshot = snapshot.canonical_json + " "
    with pytest.raises(SinaLrbRawCaptureError) as duplicate_error:
        raw_capture._sina_lrb_request_identity(
            _task(),
            synthetic_request,
            expected_run_key=identity.idempotency_key,
            snapshot_json=duplicate_key_snapshot,
        )
    assert duplicate_error.value.code == "invalid_request"

    with pytest.raises(SinaLrbRawCaptureError) as canonical_error:
        raw_capture._sina_lrb_request_identity(
            _task(),
            synthetic_request,
            expected_run_key=identity.idempotency_key,
            snapshot_json=noncanonical_snapshot,
        )
    assert canonical_error.value.code == "invalid_request"

    with pytest.raises(SinaLrbRawCaptureError) as type_error:
        raw_capture._sina_lrb_request_identity(
            _task(),
            synthetic_request,
            expected_run_key=identity.idempotency_key,
            snapshot_json=True,  # type: ignore[arg-type]
        )
    assert type_error.value.code == "invalid_request"

    with pytest.raises(SinaLrbRawCaptureError) as run_key_error:
        raw_capture._sina_lrb_request_identity(
            _task(),
            synthetic_request,
            expected_run_key="0" * 64,
            snapshot_json=snapshot.canonical_json,
        )
    assert run_key_error.value.code == "invalid_request"

    changed_job = json.loads(snapshot.canonical_json)
    changed_job["job"]["provider"] = "other-provider"
    changed_job_snapshot = json.dumps(
        changed_job, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    with pytest.raises(SinaLrbRawCaptureError) as task_error:
        raw_capture._sina_lrb_request_identity(
            _task(),
            synthetic_request,
            expected_run_key=identity.idempotency_key,
            snapshot_json=changed_job_snapshot,
        )
    assert task_error.value.code == "invalid_request"

    changed_run = json.loads(snapshot.canonical_json)
    changed_run["run"]["business_request"]["partition"]["instrument_id"] = "000001.SZ"
    changed_run_snapshot = json.dumps(
        changed_run, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    with pytest.raises(SinaLrbRawCaptureError) as request_error:
        raw_capture._sina_lrb_request_identity(
            _task(),
            synthetic_request,
            expected_run_key=identity.idempotency_key,
            snapshot_json=changed_run_snapshot,
        )
    assert request_error.value.code == "invalid_request"


def test_archived_capture_rejects_boolean_and_nonincreasing_fencing_tokens() -> None:
    """Archived DTO construction never treats bool as a fence or accepts self-origin."""
    request = _request()
    identity = _identity(request)
    base = SinaLrbArchivedCapture(
        instrument_id="600519.SH",
        run_key=identity.idempotency_key,
        status_code=200,
        retrieved_at_utc=_RETRIEVED,
        captured_at_utc=_NOW,
        request_summary_sha256=sina_lrb_request_summary_sha256("600519.SH"),
        response_sha256=hashlib.sha256(_BODY).hexdigest(),
        manifest_sha256="c" * 64,
        source_access_ref="source-auth:offline-extra",
        raw_retention_ref="raw-retention:offline-extra",
        request_payload_sha256=identity.payload_hash,
        fencing_token=2,
    )
    for update in (
        {"fencing_token": True},
        {"origin_fencing_token": 2},
        {"origin_fencing_token": True},
    ):
        with pytest.raises(ValueError):
            replace(base, **update)


def test_reuse_selection_accepts_duplicate_equivalent_capture_receipts(tmp_path: Path) -> None:
    """Repeated equivalent signed receipts remain unambiguous for one fence."""
    request = _request()
    identity = _identity(request)
    journal = SinaLrbCaptureRecoveryJournal(tmp_path / "private-raw", _Signer())
    journal._append_signed_event(_attempt_payload(request=request))
    first_payload = _capture_payload(request=request)
    journal._append_signed_event(first_payload)
    repeated = dict(first_payload)
    repeated["created_at_utc"] = raw_capture._format_utc(_NOW + timedelta(seconds=1))
    journal._append_signed_event(repeated)

    selected = journal.reusable_capture(
        run_key=identity.idempotency_key,
        request_payload_sha256=identity.payload_hash,
        request_summary_sha256=sina_lrb_request_summary_sha256("600519.SH"),
        instrument_id="600519.SH",
        current_fencing_token=1,
    )
    assert selected is not None
    assert selected.manifest_sha256 == first_payload["manifest_sha256"]


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("queued", "no_journal_evidence"),
        ("cancelled", "no_journal_evidence"),
        ("succeeded", "ledger_succeeded_link_requires_review"),
        ("running", "no_journal_evidence"),
    ],
)
def test_audit_report_classifies_empty_journal_without_guessing(
    tmp_path: Path, status: str, expected: str
) -> None:
    """A ledger status with no journal rows is reported conservatively."""
    identity = _identity()
    journal = SinaLrbCaptureRecoveryJournal(tmp_path / status, _Signer())
    report = journal.audit_run(_record(identity, status))
    assert report.disposition == expected
    assert report.capture_manifest_sha256s == ()
    assert report.attempt_fencing_tokens == ()


def test_audit_distinguishes_successful_capture_link_and_nonterminal_capture(
    tmp_path: Path,
) -> None:
    """Successful ledger rows need a reviewed join; failed rows retain orphan candidates."""
    request = _request()
    identity = _identity(request)
    handler, _delegate = _handler(tmp_path / "private-raw", signer=_Signer())
    archived = handler(request, _context(1, request))
    journal = SinaLrbCaptureRecoveryJournal(tmp_path / "private-raw", _Signer())

    succeeded = journal.audit_run(_record(identity, "succeeded"))
    assert succeeded.disposition == "ledger_succeeded_link_requires_review"
    assert succeeded.capture_manifest_sha256s == (archived.manifest_sha256,)

    running = journal.audit_run(_record(identity, "running"))
    assert running.disposition == "capture_present_on_nonterminal_run"

    failed = journal.audit_run(_record(identity, "failed"))
    assert failed.disposition == "unpublished_capture_candidate"


def test_private_journal_rejects_wrong_directory_and_lock_modes(tmp_path: Path) -> None:
    """A journal refuses shared directory permissions and symlink lock files."""
    request = _request()
    identity = _identity(request)
    root = tmp_path / "private-raw"
    journal = SinaLrbCaptureRecoveryJournal(root, _Signer())
    journal.record_attempt_started(
        run_key=identity.idempotency_key,
        request_payload_sha256=identity.payload_hash,
        request_summary_sha256=sina_lrb_request_summary_sha256("600519.SH"),
        instrument_id="600519.SH",
        fencing_token=1,
    )
    event_directory = root / "lrbjournal" / "runs" / identity.idempotency_key / "events"
    event_directory.chmod(0o755)
    with pytest.raises(SinaLrbRawCaptureError) as directory_error:
        journal.events_for_run(identity.idempotency_key)
    assert directory_error.value.code == "recovery_unavailable"
    event_directory.chmod(0o700)

    lock = event_directory.parent / "write.lock"
    lock.unlink()
    outside_target = tmp_path / "outside-lock-target"
    outside_target.write_bytes(b"private lock target")
    lock.symlink_to(outside_target)
    with pytest.raises(SinaLrbRawCaptureError) as lock_error:
        journal.events_for_run(identity.idempotency_key)
    assert lock_error.value.code == "recovery_unavailable"

    lock.unlink()
    lock.write_bytes(b"shared-mode lock")
    lock.chmod(0o644)
    with pytest.raises(SinaLrbRawCaptureError) as mode_error:
        journal.events_for_run(identity.idempotency_key)
    assert mode_error.value.code == "recovery_unavailable"


@pytest.mark.parametrize("bad_name", ["unrecognized", ".tmp-bad-shape", "0" * 64 + ".txt"])
def test_journal_rejects_unknown_entries_and_unrecognized_crash_temps(
    tmp_path: Path, bad_name: str
) -> None:
    """Recovery never adopts unknown files from its owner-private event folder."""
    request = _request()
    identity = _identity(request)
    journal = SinaLrbCaptureRecoveryJournal(tmp_path / "private-raw", _Signer())
    journal.record_attempt_started(
        run_key=identity.idempotency_key,
        request_payload_sha256=identity.payload_hash,
        request_summary_sha256=sina_lrb_request_summary_sha256("600519.SH"),
        instrument_id="600519.SH",
        fencing_token=1,
    )
    event_directory = (
        tmp_path / "private-raw" / "lrbjournal" / "runs" / identity.idempotency_key / "events"
    )
    (event_directory / bad_name).write_bytes(b"not a journal record")
    with pytest.raises(SinaLrbRawCaptureError) as error:
        journal.events_for_run(identity.idempotency_key)
    assert error.value.code == "recovery_invalid"


def test_journal_rejects_mismatched_event_digest_and_nonprivate_event_file(tmp_path: Path) -> None:
    """A content address and owner-private mode are checked before parsing bytes."""
    identity = _identity()
    journal = SinaLrbCaptureRecoveryJournal(tmp_path / "private-raw", _Signer())
    journal.record_attempt_started(
        run_key=identity.idempotency_key,
        request_payload_sha256=identity.payload_hash,
        request_summary_sha256=sina_lrb_request_summary_sha256("600519.SH"),
        instrument_id="600519.SH",
        fencing_token=1,
    )
    event_directory = (
        tmp_path / "private-raw" / "lrbjournal" / "runs" / identity.idempotency_key / "events"
    )
    event_path = next(event_directory.glob("*.json"))
    encoded = event_path.read_bytes()
    event_path.unlink()
    wrong_digest_path = event_directory / f"{'0' * 64}.json"
    wrong_digest_path.write_bytes(encoded)
    wrong_digest_path.chmod(0o600)
    with pytest.raises(SinaLrbRawCaptureError) as digest_error:
        journal.events_for_run(identity.idempotency_key)
    assert digest_error.value.code == "recovery_invalid"

    wrong_digest_path.unlink()
    event_path.write_bytes(encoded)
    event_path.chmod(0o644)
    with pytest.raises(SinaLrbRawCaptureError) as mode_error:
        journal.events_for_run(identity.idempotency_key)
    assert mode_error.value.code == "recovery_invalid"


def test_journal_rejects_validly_signed_event_stored_under_another_run(tmp_path: Path) -> None:
    """A valid signature does not let an event move between run directories."""
    request = _request()
    identity = _identity(request)
    journal = SinaLrbCaptureRecoveryJournal(tmp_path / "private-raw", _Signer())
    journal.record_attempt_started(
        run_key=identity.idempotency_key,
        request_payload_sha256=identity.payload_hash,
        request_summary_sha256=sina_lrb_request_summary_sha256("600519.SH"),
        instrument_id="600519.SH",
        fencing_token=1,
    )
    foreign_request = _request("different-logical-run")
    foreign_payload = _attempt_payload(request=foreign_request)
    foreign_encoded = _encode_signed(foreign_payload)
    event_directory = (
        tmp_path / "private-raw" / "lrbjournal" / "runs" / identity.idempotency_key / "events"
    )
    foreign_path = event_directory / f"{hashlib.sha256(foreign_encoded).hexdigest()}.json"
    foreign_path.write_bytes(foreign_encoded)
    foreign_path.chmod(0o600)

    with pytest.raises(SinaLrbRawCaptureError) as error:
        journal.events_for_run(identity.idempotency_key)
    assert error.value.code == "recovery_invalid"


def test_journal_rejects_symlinked_event_and_unprivate_crash_temporary(tmp_path: Path) -> None:
    """Reads never follow event links and only clean owner-private crash temps."""
    identity = _identity()
    root = tmp_path / "private-raw"
    journal = SinaLrbCaptureRecoveryJournal(root, _Signer())
    journal.record_attempt_started(
        run_key=identity.idempotency_key,
        request_payload_sha256=identity.payload_hash,
        request_summary_sha256=sina_lrb_request_summary_sha256("600519.SH"),
        instrument_id="600519.SH",
        fencing_token=1,
    )
    event_directory = root / "lrbjournal" / "runs" / identity.idempotency_key / "events"
    event_path = next(event_directory.glob("*.json"))
    event_bytes = event_path.read_bytes()
    event_path.unlink()
    outside_event = tmp_path / "outside-event"
    outside_event.write_bytes(event_bytes)
    outside_event.chmod(0o600)
    event_path.symlink_to(outside_event)
    with pytest.raises(SinaLrbRawCaptureError) as link_error:
        journal.events_for_run(identity.idempotency_key)
    assert link_error.value.code == "recovery_unavailable"

    event_path.unlink()
    event_path.write_bytes(event_bytes)
    event_path.chmod(0o600)
    temporary = event_directory / ".tmp-0123456789abcdef0123456789abcdef"
    temporary.write_bytes(b"left before publish")
    temporary.chmod(0o644)
    with pytest.raises(SinaLrbRawCaptureError) as temp_error:
        journal.events_for_run(identity.idempotency_key)
    assert temp_error.value.code == "recovery_unavailable"


def test_capture_verification_rejects_untrusted_dto_and_mismatched_receipt(
    tmp_path: Path,
) -> None:
    """Verification binds the body-free DTO to its signed event and attempt."""
    request = _request()
    handler, _delegate = _handler(tmp_path / "private-raw", signer=_Signer())
    archived = handler(request, _context(1, request))
    identity = _identity(request)
    journal = SinaLrbCaptureRecoveryJournal(tmp_path / "private-raw", _Signer())

    with pytest.raises(SinaLrbRawCaptureError) as dto_error:
        journal.verify_capture_receipt(
            object(),
            request_payload_sha256=identity.payload_hash,
            fencing_token=1,
        )  # type: ignore[arg-type]
    assert dto_error.value.code == "recovery_invalid"

    with pytest.raises(SinaLrbRawCaptureError) as fence_error:
        journal.verify_capture_receipt(
            archived,
            request_payload_sha256=identity.payload_hash,
            fencing_token=True,  # type: ignore[arg-type]
        )
    assert fence_error.value.code == "recovery_invalid"

    with pytest.raises(SinaLrbRawCaptureError) as request_error:
        journal.verify_capture_receipt(
            archived,
            request_payload_sha256="0" * 64,
            fencing_token=1,
        )
    assert request_error.value.code == "recovery_invalid"


def test_recovery_helpers_initialize_missing_root_and_reject_invalid_run_keys(
    tmp_path: Path,
) -> None:
    """A missing private root is initialized before reads and malformed keys fail early."""
    missing_root = tmp_path / "missing-root"
    journal = SinaLrbCaptureRecoveryJournal(missing_root, _Signer())
    assert journal.events_for_run("a" * 64) == ()
    assert missing_root.is_dir()
    assert missing_root.stat().st_mode & 0o777 == 0o700

    for bad_run_key in ("not-a-digest", "A" * 64, "../" + "a" * 61):
        with pytest.raises(SinaLrbRawCaptureError) as invalid_error:
            journal.events_for_run(bad_run_key)
        assert invalid_error.value.code == "invalid_request"


def test_recovery_helpers_reject_existing_nonprivate_root(tmp_path: Path) -> None:
    """An existing shared artifact root is not silently changed or accepted."""
    nonprivate_root = tmp_path / "nonprivate-root"
    nonprivate_root.mkdir(mode=0o755)
    nonprivate_root.chmod(0o755)
    journal = SinaLrbCaptureRecoveryJournal(nonprivate_root, _Signer())
    with pytest.raises(SinaLrbRawCaptureError) as unavailable_error:
        journal.events_for_run("b" * 64)
    assert unavailable_error.value.code == "recovery_unavailable"


def test_append_rejects_a_capture_receipt_without_a_valid_origin_attempt(tmp_path: Path) -> None:
    """The append boundary independently checks same-run fence and origin chains."""
    journal = SinaLrbCaptureRecoveryJournal(tmp_path / "private-raw", _Signer())
    payload = _capture_payload(fencing_token=2, origin_fencing_token=1)
    with pytest.raises(SinaLrbRawCaptureError) as error:
        journal._append_signed_event(payload)
    assert error.value.code == "recovery_invalid"
    identity = _identity()
    assert journal.events_for_run(identity.idempotency_key) == ()


def test_record_capture_rejects_wrong_archive_type_and_checkpoint_loss(tmp_path: Path) -> None:
    """Journal capture linking requires the exact DTO and a live context checkpoint."""
    identity = _identity()
    journal = SinaLrbCaptureRecoveryJournal(tmp_path / "private-raw", _Signer())
    with pytest.raises(SinaLrbRawCaptureError) as dto_error:
        journal.record_capture(
            object(),  # type: ignore[arg-type]
            task=_task(),
            request=_request(),
            request_payload_sha256=identity.payload_hash,
            fencing_token=1,
        )
    assert dto_error.value.code == "invalid_capture"

    handler, _delegate = _handler(tmp_path / "capture-root", signer=None)
    archived = handler(_request(), _context(1))
    checkpoint_context = RunExecutionContext(
        run_key=identity.idempotency_key,
        worker="offline-worker",
        fencing_token=1,
        _checkpoint=lambda: (_ for _ in ()).throw(RuntimeError("lease lost")),
    )
    with pytest.raises(SinaLrbRawCaptureError) as context_error:
        journal.record_capture(
            archived,
            task=_task(),
            request=_request(),
            request_payload_sha256=identity.payload_hash,
            fencing_token=1,
            context=checkpoint_context,
        )
    assert context_error.value.code == "recovery_invalid"


def test_record_capture_without_matching_started_attempt_is_not_linked(tmp_path: Path) -> None:
    """An archived manifest alone cannot create a capture receipt without a started attempt."""
    request = _request()
    identity = _identity(request)
    root = tmp_path / "capture-root"
    handler, _delegate = _handler(root)
    original = handler(request, _context(1, request))
    archived = replace(
        original,
        request_payload_sha256=identity.payload_hash,
        fencing_token=1,
    )
    journal = SinaLrbCaptureRecoveryJournal(root, _Signer())

    with pytest.raises(SinaLrbRawCaptureError) as error:
        journal.record_capture(
            archived,
            task=_task(),
            request=request,
            request_payload_sha256=identity.payload_hash,
            fencing_token=1,
        )

    assert error.value.code == "recovery_invalid"
    assert journal.events_for_run(identity.idempotency_key) == ()
