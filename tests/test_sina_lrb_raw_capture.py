"""Offline synthetic coverage for the explicit Sina LRB raw-capture adapter."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone, tzinfo
from typing import TYPE_CHECKING

import pytest

import opendata.pipeline.sina_lrb_raw_capture as raw_capture
from opendata.data.providers.sina.lrb_statement_process import SinaLrbProcessError, SinaLrbQuery
from opendata.pipeline.raw_evidence import (
    CapturedRawResponse,
    HistoryLimit,
    RawResponseArtifactWriter,
    RawResponseSource,
    RetentionClass,
)
from opendata.pipeline.raw_evidence_reader import RawEvidenceReadResult, RawResponseArtifactReader
from opendata.pipeline.reviewed_worker import ProcessFetchHandler
from opendata.pipeline.run_identity import RunRequest
from opendata.pipeline.run_service import RunExecutionContext
from opendata.pipeline.sina_lrb_process_job import SinaLrbProcessCapture
from opendata.pipeline.sina_lrb_raw_capture import (
    SINA_LRB_RAW_RESPONSE_SOURCE,
    SinaLrbArchivedCapture,
    SinaLrbRawAuthorization,
    SinaLrbRawCaptureError,
    SinaLrbRawCaptureHandler,
    build_sina_lrb_query_parameters,
    read_sina_lrb_archived_response,
    sina_lrb_request_summary_sha256,
)

if TYPE_CHECKING:
    from pathlib import Path

_NOW = datetime(2020, 1, 2, 3, 4, tzinfo=timezone.utc)
_BODY = b'{"synthetic":"sina-lrb-response"}'
_GOLDEN_REQUEST_SHA256 = "3efc9094f5d1bcb908eb943d40dae109f4e571710766edc8bdadeccb64a36b70"


class _BrokenTimezone(tzinfo):
    def utcoffset(self, dt: datetime | None) -> timedelta:
        del dt
        raise RuntimeError("timezone detail must not escape")

    def dst(self, dt: datetime | None) -> timedelta:
        del dt
        return timedelta(0)


class _FixedProcessHandler(ProcessFetchHandler):
    """In-memory reviewed-handler stand-in that never starts a child process."""

    def __init__(self, result: object, *, interval: float = 0.05) -> None:
        self.result = result
        self.calls = 0
        self._interval = interval

    @property
    def heartbeat_interval_seconds(self) -> float:
        return self._interval

    def __call__(self, request: RunRequest, context: RunExecutionContext) -> object:
        del request, context
        self.calls += 1
        return self.result


def _capture(
    *,
    instrument_id: str = "600519.SH",
    status_code: int = 200,
    retrieved_at: datetime = _NOW,
    body: bytes = _BODY,
) -> SinaLrbProcessCapture:
    return SinaLrbProcessCapture(
        query=SinaLrbQuery(instrument_id),
        status_code=status_code,
        retrieved_at_utc=retrieved_at,
        body=body,
    )


def _authorization(
    *, source_access: bool = True, raw_retention: bool = True
) -> SinaLrbRawAuthorization:
    return SinaLrbRawAuthorization(
        source_access_ref="source-auth:synthetic-grant-1",
        raw_retention_ref="raw-retention:synthetic-grant-1",
        source_access_authorized=source_access,
        raw_retention_authorized=raw_retention,
    )


def _request(instrument_id: str = "600519.SH") -> RunRequest:
    return RunRequest(
        run_type="manual",
        query={},
        partition={"instrument_id": instrument_id},
        shard=None,
        idempotency_key="a" * 64,
    )


def _context() -> RunExecutionContext:
    return RunExecutionContext(
        run_key="b" * 64,
        worker="offline-test-worker",
        fencing_token=1,
        _checkpoint=lambda: None,
    )


def _archived_capture() -> SinaLrbArchivedCapture:
    return SinaLrbArchivedCapture(
        instrument_id="600519.SH",
        run_key="b" * 64,
        status_code=200,
        retrieved_at_utc=_NOW,
        captured_at_utc=_NOW,
        request_summary_sha256=_GOLDEN_REQUEST_SHA256,
        response_sha256=hashlib.sha256(_BODY).hexdigest(),
        manifest_sha256="c" * 64,
        source_access_ref="source-auth:review-1",
        raw_retention_ref="raw-retention:review-1",
    )


def _handler(
    result: object,
    root: Path,
    *,
    authorization: SinaLrbRawAuthorization | None = None,
) -> tuple[SinaLrbRawCaptureHandler, _FixedProcessHandler]:
    delegate = _FixedProcessHandler(result)
    wrapped = SinaLrbRawCaptureHandler(
        delegate,
        artifact_root=root,
        authorization=authorization or _authorization(),
        retention_class="restricted",
        source_history_limit="unknown",
    )
    return wrapped, delegate


def test_fixed_query_and_request_summary_golden_vector() -> None:
    assert build_sina_lrb_query_parameters("600519.SH") == {
        "paperCode": "sh600519",
        "source": "lrb",
        "type": "0",
        "page": "1",
        "num": "1000",
    }
    assert build_sina_lrb_query_parameters("000001.SZ") == {
        "paperCode": "sz000001",
        "source": "lrb",
        "type": "0",
        "page": "1",
        "num": "1000",
    }
    assert sina_lrb_request_summary_sha256("600519.SH") == _GOLDEN_REQUEST_SHA256
    assert sina_lrb_request_summary_sha256("000001.SZ") != _GOLDEN_REQUEST_SHA256


@pytest.mark.parametrize("instrument_id", ["600519.BJ", "600519", " sh600519"])
def test_query_builder_rejects_noncanonical_symbols(instrument_id: str) -> None:
    with pytest.raises(SinaLrbProcessError) as error:
        build_sina_lrb_query_parameters(instrument_id)

    assert error.value.code == "invalid_query"


@pytest.mark.parametrize(
    "authorization",
    [
        _authorization(source_access=False),
        _authorization(raw_retention=False),
    ],
)
def test_authorization_denial_happens_before_process_handler_or_filesystem(
    authorization: SinaLrbRawAuthorization,
    tmp_path: Path,
) -> None:
    root = tmp_path / "raw"
    handler, delegate = _handler(
        _capture(),
        root,
        authorization=authorization,
    )

    with pytest.raises(SinaLrbRawCaptureError) as error:
        handler(_request(), _context())

    assert error.value.code == "authorization_denied"
    assert delegate.calls == 0
    assert not root.exists()


def test_raw_capture_requires_explicit_private_root_and_declared_retention(tmp_path: Path) -> None:
    delegate = _FixedProcessHandler(_capture())
    authorization = _authorization()

    with pytest.raises(ValueError):
        SinaLrbRawCaptureHandler(
            delegate,
            artifact_root="relative/raw",
            authorization=authorization,
            retention_class="restricted",
            source_history_limit="unknown",
        )
    with pytest.raises(ValueError, match="retention_class is invalid"):
        SinaLrbRawCaptureHandler(
            delegate,
            artifact_root=tmp_path / "raw",
            authorization=authorization,
            retention_class="unclassified",  # type: ignore[arg-type]
            source_history_limit="unknown",
        )
    with pytest.raises(ValueError, match="source_history_limit is invalid"):
        SinaLrbRawCaptureHandler(
            delegate,
            artifact_root=tmp_path / "raw",
            authorization=authorization,
            retention_class="restricted",
            source_history_limit="unbounded",  # type: ignore[arg-type]
        )


def test_raw_capture_rejects_non_process_delegate_and_wrong_authorization_type(
    tmp_path: Path,
) -> None:
    with pytest.raises(TypeError, match="delegate must be a ProcessFetchHandler"):
        SinaLrbRawCaptureHandler(
            object(),  # type: ignore[arg-type]
            artifact_root=tmp_path / "raw",
            authorization=_authorization(),
            retention_class="restricted",
            source_history_limit="unknown",
        )

    with pytest.raises(TypeError, match="authorization must be a SinaLrbRawAuthorization"):
        SinaLrbRawCaptureHandler(
            _FixedProcessHandler(_capture()),
            artifact_root=tmp_path / "raw",
            authorization=object(),  # type: ignore[arg-type]
            retention_class="restricted",
            source_history_limit="unknown",
        )


def test_raw_capture_error_codes_are_closed_and_stable() -> None:
    with pytest.raises(ValueError, match="Unsupported Sina LRB raw-capture error code"):
        SinaLrbRawCaptureError("not-a-code")  # type: ignore[arg-type]


def test_archived_result_is_body_free_and_reader_verifies_exact_bytes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "private-raw"
    handler, delegate = _handler(_capture(), root)

    result = handler(_request(), _context())

    assert isinstance(handler, ProcessFetchHandler)
    assert handler.heartbeat_interval_seconds == delegate.heartbeat_interval_seconds
    assert type(result) is SinaLrbArchivedCapture
    assert not hasattr(result, "body")
    assert result.instrument_id == "600519.SH"
    assert result.run_key == "b" * 64
    assert result.status_code == 200
    assert result.request_summary_sha256 == _GOLDEN_REQUEST_SHA256
    assert result.response_sha256 == hashlib.sha256(_BODY).hexdigest()
    assert result.source_access_ref == "source-auth:synthetic-grant-1"
    assert result.raw_retention_ref == "raw-retention:synthetic-grant-1"
    assert delegate.calls == 1

    reader = RawResponseArtifactReader(root)
    evidence = reader.read(
        result.manifest_sha256,
        expected_source=SINA_LRB_RAW_RESPONSE_SOURCE,
        expected_request_summary_sha256=result.request_summary_sha256,
        captured_at_not_after=datetime.now(timezone.utc),
    )
    assert evidence.body == _BODY
    assert evidence.manifest.response_sha256 == result.response_sha256
    assert evidence.manifest.response_size_bytes == len(_BODY)
    assert evidence.manifest.retention_class == "restricted"
    assert evidence.manifest.source_history_limit == "unknown"
    assert read_sina_lrb_archived_response(result, artifact_root=root) == _BODY

    with pytest.raises(SinaLrbRawCaptureError) as error:
        read_sina_lrb_archived_response(
            replace(result, response_sha256="d" * 64),
            artifact_root=root,
        )

    assert error.value.code == "archive_failed"


def test_wrong_instrument_is_rejected_before_raw_storage(tmp_path: Path) -> None:
    root = tmp_path / "raw"
    handler, delegate = _handler(_capture(instrument_id="000001.SZ"), root)

    with pytest.raises(SinaLrbRawCaptureError) as error:
        handler(_request("600519.SH"), _context())

    assert error.value.code == "invalid_capture"
    assert delegate.calls == 1
    assert not root.exists()


def test_wrong_status_is_rejected_before_raw_storage(tmp_path: Path) -> None:
    root = tmp_path / "raw"
    handler, delegate = _handler(_capture(status_code=503), root)

    with pytest.raises(SinaLrbRawCaptureError) as error:
        handler(_request(), _context())

    assert error.value.code == "invalid_capture"
    assert delegate.calls == 1
    assert not root.exists()


@pytest.mark.parametrize(
    "capture",
    [
        object(),
        SinaLrbProcessCapture(
            query=object(),  # type: ignore[arg-type]
            status_code=200,
            retrieved_at_utc=_NOW,
            body=_BODY,
        ),
        SinaLrbProcessCapture(
            query=SinaLrbQuery("600519.SH"),
            status_code=True,  # type: ignore[arg-type]
            retrieved_at_utc=_NOW,
            body=_BODY,
        ),
        SinaLrbProcessCapture(
            query=SinaLrbQuery("600519.SH"),
            status_code=200,
            retrieved_at_utc=datetime(2020, 1, 2, 3, 4),
            body=_BODY,
        ),
        SinaLrbProcessCapture(
            query=SinaLrbQuery("600519.SH"),
            status_code=200,
            retrieved_at_utc=datetime(
                2020,
                1,
                2,
                3,
                4,
                tzinfo=timezone(timedelta(hours=1)),
            ),
            body=_BODY,
        ),
        SinaLrbProcessCapture(
            query=SinaLrbQuery("600519.SH"),
            status_code=200,
            retrieved_at_utc=datetime(2020, 1, 2, tzinfo=_BrokenTimezone()),
            body=_BODY,
        ),
        SinaLrbProcessCapture(
            query=SinaLrbQuery("600519.SH"),
            status_code=200,
            retrieved_at_utc=_NOW,
            body=bytearray(_BODY),  # type: ignore[arg-type]
        ),
    ],
)
def test_untrusted_or_noncanonical_process_results_fail_before_storage(
    capture: object,
    tmp_path: Path,
) -> None:
    root = tmp_path / "raw"
    handler, _delegate = _handler(capture, root)

    with pytest.raises(SinaLrbRawCaptureError) as error:
        handler(_request(), _context())

    assert error.value.code == "invalid_capture"
    assert not root.exists()


def test_reader_detects_mutated_content_addressed_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_capture = RawResponseArtifactWriter.capture

    def capture_then_mutate(
        writer: RawResponseArtifactWriter,
        chunks: tuple[bytes, ...],
        *,
        source: RawResponseSource,
        request_summary_sha256: str,
        http_status: int,
        retention_class: RetentionClass,
        source_history_limit: HistoryLimit,
    ) -> CapturedRawResponse:
        captured = original_capture(
            writer,
            chunks,
            source=source,
            request_summary_sha256=request_summary_sha256,
            http_status=http_status,
            retention_class=retention_class,
            source_history_limit=source_history_limit,
        )
        captured.artifact_path.write_bytes(b"mutated-after-capture")
        return captured

    monkeypatch.setattr(RawResponseArtifactWriter, "capture", capture_then_mutate)
    handler, delegate = _handler(_capture(), tmp_path / "private-raw")

    with pytest.raises(SinaLrbRawCaptureError) as error:
        handler(_request(), _context())

    assert error.value.code == "archive_failed"
    assert delegate.calls == 1


def test_source_and_retention_authorization_references_are_required() -> None:
    with pytest.raises(ValueError):
        SinaLrbRawAuthorization(
            source_access_ref="missing-prefix",
            raw_retention_ref="raw-retention:approved-1",
            source_access_authorized=True,
            raw_retention_authorized=True,
        )


@pytest.mark.parametrize(
    ("source_access_ref", "raw_retention_ref"),
    [
        ("source-auth:sk-example-0123456789abcdef", "raw-retention:review-1"),
        ("source-auth:review-1", "raw-retention:AKIA1234567890ABCDEF"),
    ],
)
def test_authorization_references_reject_secret_shaped_values(
    source_access_ref: str,
    raw_retention_ref: str,
) -> None:
    with pytest.raises(ValueError):
        SinaLrbRawAuthorization(
            source_access_ref=source_access_ref,
            raw_retention_ref=raw_retention_ref,
            source_access_authorized=True,
            raw_retention_authorized=True,
        )


def test_archived_capture_dto_revalidates_request_and_timestamp_binding() -> None:
    result = SinaLrbArchivedCapture(
        instrument_id="600519.SH",
        run_key="b" * 64,
        status_code=200,
        retrieved_at_utc=_NOW,
        captured_at_utc=_NOW,
        request_summary_sha256=_GOLDEN_REQUEST_SHA256,
        response_sha256=hashlib.sha256(_BODY).hexdigest(),
        manifest_sha256="c" * 64,
        source_access_ref="source-auth:review-1",
        raw_retention_ref="raw-retention:review-1",
    )

    assert type(result) is SinaLrbArchivedCapture
    assert not hasattr(result, "body")

    with pytest.raises(ValueError):
        SinaLrbArchivedCapture(
            instrument_id="600519.SH",
            run_key="b" * 64,
            status_code=200,
            retrieved_at_utc=_NOW,
            captured_at_utc=datetime(2020, 1, 2, 2, 59, tzinfo=timezone.utc),
            request_summary_sha256=_GOLDEN_REQUEST_SHA256,
            response_sha256=hashlib.sha256(_BODY).hexdigest(),
            manifest_sha256="c" * 64,
            source_access_ref="source-auth:review-1",
            raw_retention_ref="raw-retention:review-1",
        )

    with pytest.raises(ValueError):
        SinaLrbArchivedCapture(
            instrument_id="600519.SH",
            run_key="b" * 64,
            status_code=200,
            retrieved_at_utc=_NOW,
            captured_at_utc=_NOW,
            request_summary_sha256="d" * 64,
            response_sha256=hashlib.sha256(_BODY).hexdigest(),
            manifest_sha256="c" * 64,
            source_access_ref="source-auth:review-1",
            raw_retention_ref="raw-retention:review-1",
        )


def test_archived_capture_rejects_invalid_instrument_identity() -> None:
    with pytest.raises(ValueError, match="archived-capture metadata is invalid"):
        replace(_archived_capture(), instrument_id="600519.BJ")


@pytest.mark.parametrize(
    "timestamp",
    [
        datetime(2020, 1, 2, 3, 4),
        datetime(2020, 1, 2, 3, 4, tzinfo=timezone(timedelta(hours=1))),
        datetime(2020, 1, 2, 3, 4, tzinfo=_BrokenTimezone()),
    ],
)
def test_archived_capture_requires_valid_utc_observation_times(timestamp: datetime) -> None:
    with pytest.raises(ValueError, match="archived-capture metadata is invalid"):
        replace(_archived_capture(), retrieved_at_utc=timestamp)


def test_archived_reader_rejects_untrusted_dto_and_unavailable_manifest(
    tmp_path: Path,
) -> None:
    with pytest.raises(SinaLrbRawCaptureError) as bad_dto:
        raw_capture.read_sina_lrb_archived_evidence(
            object(),  # type: ignore[arg-type]
            artifact_root=tmp_path / "raw",
        )
    assert bad_dto.value.code == "invalid_capture"

    with pytest.raises(SinaLrbRawCaptureError) as missing_manifest:
        raw_capture.read_sina_lrb_archived_evidence(
            _archived_capture(),
            artifact_root=tmp_path / "raw",
        )
    assert missing_manifest.value.code == "archive_failed"


def test_capture_handler_rejects_invalid_context_ownership_before_fetch_or_storage(
    tmp_path: Path,
) -> None:
    root = tmp_path / "raw"
    handler, delegate = _handler(_capture(), root)
    malformed_contexts = (
        object(),
        RunExecutionContext(
            run_key="invalid",
            worker="offline-test-worker",
            fencing_token=1,
            _checkpoint=lambda: None,
        ),
        RunExecutionContext(
            run_key="b" * 64,
            worker="",
            fencing_token=1,
            _checkpoint=lambda: None,
        ),
        RunExecutionContext(
            run_key="b" * 64,
            worker="offline-test-worker",
            fencing_token=0,
            _checkpoint=lambda: None,
        ),
    )

    for context in malformed_contexts:
        with pytest.raises(SinaLrbRawCaptureError) as error:
            handler(_request(), context)  # type: ignore[arg-type]
        assert error.value.code == "invalid_request"

    assert delegate.calls == 0
    assert not root.exists()


@pytest.mark.parametrize(
    "raw_request",
    [
        object(),
        _request().model_copy(update={"run_type": "unsupported"}),
        _request().model_copy(update={"query": {"source": "unexpected"}}),
        _request().model_copy(update={"partition": {"instrument_id": "600519.SH", "other": "x"}}),
        _request().model_copy(update={"partition": {"instrument_id": "600519.BJ"}}),
    ],
)
def test_capture_handler_rejects_invalid_manual_request_shapes_before_fetch_or_storage(
    raw_request: object,
    tmp_path: Path,
) -> None:
    root = tmp_path / "raw"
    handler, delegate = _handler(_capture(), root)

    with pytest.raises(SinaLrbRawCaptureError) as error:
        handler(raw_request, _context())  # type: ignore[arg-type]

    assert error.value.code == "invalid_request"
    assert delegate.calls == 0
    assert not root.exists()


def test_capture_handler_fails_closed_if_request_digest_cannot_be_built(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fail_digest(_instrument_id: str) -> str:
        raise RuntimeError("synthetic digest failure")

    monkeypatch.setattr(raw_capture, "sina_lrb_request_summary_sha256", fail_digest)
    root = tmp_path / "raw"
    handler, delegate = _handler(_capture(), root)

    with pytest.raises(SinaLrbRawCaptureError) as error:
        handler(_request(), _context())

    assert error.value.code == "request_summary_invalid"
    assert delegate.calls == 0
    assert not root.exists()


def test_capture_handler_rechecks_reader_result_before_returning_reference(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    original_read = RawResponseArtifactReader.read

    def read_with_substituted_bytes(
        reader: RawResponseArtifactReader,
        manifest_sha256: str,
        *,
        expected_source: RawResponseSource,
        expected_request_summary_sha256: str,
        captured_at_not_after: datetime,
        expected_http_status: int = 200,
    ) -> RawEvidenceReadResult:
        verified = original_read(
            reader,
            manifest_sha256,
            expected_source=expected_source,
            expected_request_summary_sha256=expected_request_summary_sha256,
            captured_at_not_after=captured_at_not_after,
            expected_http_status=expected_http_status,
        )
        return replace(verified, body=b"substituted after verification")

    monkeypatch.setattr(RawResponseArtifactReader, "read", read_with_substituted_bytes)
    handler, delegate = _handler(_capture(), tmp_path / "raw")

    with pytest.raises(SinaLrbRawCaptureError) as error:
        handler(_request(), _context())

    assert error.value.code == "archive_failed"
    assert delegate.calls == 1
