"""Offline behavior tests for the bounded Fuyao A-share catalog capture."""

from __future__ import annotations

import base64
import builtins
import hashlib
import html
import json
import os
import secrets
import stat
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from urllib.parse import quote

import pytest

from opendata.data.providers.ths.transport.credentials import FuyaoCredentials
from opendata.data.providers.ths.transport.http_client import FuyaoRawExchangeResponse
from opendata.data.request_budget import current_request_scopes, reserve_scoped_attempt
from scripts.ops import fuyao_a_share_catalog_capture as capture_module

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence


def _response(
    rows: Sequence[Mapping[str, object]], *, status: int = 200
) -> FuyaoRawExchangeResponse:
    """Encode one synthetic Fuyao catalog envelope for the offline client."""
    body = json.dumps(
        {"code": 0, "data": {"item": [dict(row) for row in rows]}},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return FuyaoRawExchangeResponse(status_code=status, body=body, elapsed_seconds=0.01)


def _authorization(path: Path, **overrides: object) -> Path:
    """Write one private, short-lived authorization fixture."""
    now = capture_module._utcnow()
    payload: dict[str, object] = {
        "schema_version": capture_module.AUTHORIZATION_SCHEMA_VERSION,
        "purpose": capture_module.AUTHORIZATION_PURPOSE,
        "endpoint_path": capture_module.TICKERS_LIST_ENDPOINT,
        "asset_type": "a-share",
        "page_limit": capture_module.PAGE_LIMIT,
        "max_pages": capture_module.MAX_PAGES,
        "max_requests": capture_module.MAX_REQUESTS,
        "max_response_bytes": capture_module.MAX_RESPONSE_BYTES,
        "max_total_response_bytes": capture_module.MAX_TOTAL_RESPONSE_BYTES,
        "max_task_rows": capture_module.MAX_TASK_ROWS,
        "timeout_seconds": capture_module.REQUEST_TIMEOUT_SECONDS,
        "max_task_timeout_seconds": capture_module.MAX_TASK_TIMEOUT_SECONDS,
        "issued_at_utc": capture_module._format_utc(now - timedelta(seconds=1)),
        "expires_at_utc": capture_module._format_utc(now + timedelta(minutes=5)),
        "nonce": secrets.token_hex(16),
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.chmod(path, 0o600)
    return path


class _FakeClient:
    """A fake that consumes the active request budget without doing I/O."""

    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.scope_limits: list[tuple[int, int, bool]] = []
        self.closed = False

    def get_raw_exchange(
        self, endpoint: str, *, params: Mapping[str, object]
    ) -> FuyaoRawExchangeResponse:
        """Record exact calls and simulate the real client's budget reservation."""
        scopes = current_request_scopes()
        assert len(scopes) == 1
        scope = scopes[0]
        self.scope_limits.append(
            (
                scope.budget.task_attempt_limit,
                scope.budget.source_attempt_limit,
                scope.deadline is not None,
            )
        )
        reserve_scoped_attempt(scopes, capture_module.SOURCE, capture_module.HOST)
        self.calls.append((endpoint, dict(params)))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        assert isinstance(response, FuyaoRawExchangeResponse)
        return response

    def close(self) -> None:
        """Record closure without external effects."""
        self.closed = True


def _run(
    tmp_path: Path,
    fake: _FakeClient,
    *,
    auth_overrides: dict[str, object] | None = None,
    key: str = "synthetic-test-key",
) -> tuple[dict[str, object], Path, Path]:
    """Run the command through injected offline dependencies and private paths."""
    auth_path = _authorization(tmp_path / "authorization.json", **(auth_overrides or {}))
    evidence_dir = tmp_path / "evidence"
    credentials = FuyaoCredentials(key)
    receipt = capture_module.run_catalog_capture(
        auth_path,
        evidence_dir,
        credentials=credentials,
        client_factory=lambda _credentials: fake,
        authorization_state_dir=tmp_path / "nonce-state",
    )
    return receipt, auth_path, evidence_dir


def test_two_page_capture_is_bounded_private_and_anonymous(tmp_path: Path) -> None:
    first_rows = [
        {"source_token": f"row-{index:05d}"} for index in range(capture_module.PAGE_LIMIT)
    ]
    second_rows = [{"source_token": "private-row-marker"}]
    fake = _FakeClient([_response(first_rows), _response(second_rows)])

    receipt, auth_path, evidence_dir = _run(tmp_path, fake)

    assert receipt["outcome"] == "bounded_current_snapshot_sample_captured"
    assert receipt["pages_attempted"] == 2
    assert receipt["pages_succeeded"] == 2
    assert receipt["row_count"] == capture_module.PAGE_LIMIT + 1
    assert receipt["pagination_status"] == "unknown"
    assert receipt["market_completeness"] == "not_established"
    assert receipt["historical_coverage"] == "not_established"
    assert receipt["retired_a_shares"] == "not_included_by_current_listing_source_contract"
    assert receipt["row_values_in_receipt"] is False
    assert "private-row-marker" not in json.dumps(receipt)
    assert not auth_path.exists()
    assert fake.closed is True
    assert fake.scope_limits == [(2, 2, True), (2, 2, True)]
    assert [params for _endpoint, params in fake.calls] == [
        {"asset_type": "a-share", "limit": 10_000, "offset": 0},
        {"asset_type": "a-share", "limit": 10_000, "offset": 10_000},
    ]
    assert all(endpoint == capture_module.TICKERS_LIST_ENDPOINT for endpoint, _ in fake.calls)

    raw_files = tuple((evidence_dir / "raw").rglob("*"))
    assert any(path.suffix == ".blob" for path in raw_files)
    assert any(path.suffix == ".json" for path in raw_files)
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o700 for path in raw_files if path.is_dir())
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in raw_files if path.is_file())
    for name in ("receipt.json", "page-01-receipt.json", "page-02-receipt.json"):
        summary = (evidence_dir / name).read_text(encoding="utf-8")
        assert "private-row-marker" not in summary
        assert "row-00000" not in summary


def test_short_page_stops_without_claiming_pagination_or_market_completeness(
    tmp_path: Path,
) -> None:
    fake = _FakeClient([_response([{"ticker": "synthetic-only"}])])

    receipt, _auth_path, _evidence_dir = _run(tmp_path, fake)

    assert receipt["outcome"] == "bounded_current_snapshot_sample_captured"
    assert receipt["pages_attempted"] == 1
    assert receipt["row_count"] == 1
    assert receipt["pagination_status"] == "unknown"
    assert receipt["market_completeness"] == "not_established"
    assert len(fake.calls) == 1


def test_credential_reflection_suppresses_body_before_raw_persistence(tmp_path: Path) -> None:
    secret = "synthetic-secret-marker"
    fake = _FakeClient([_response([{"echo": secret}])])

    receipt, _auth_path, evidence_dir = _run(tmp_path, fake, key=secret)

    assert receipt["outcome"] == "partial_or_failed_capture"
    assert receipt["failure_code"] == "credential_reflection_detected"
    assert receipt["page_receipts"][0]["raw_response_persisted"] is False  # type: ignore[index]
    assert receipt["page_receipts"][0]["response_suppressed"] is True  # type: ignore[index]
    assert not (evidence_dir / "raw").exists()
    persisted = "".join(path.read_text(encoding="utf-8") for path in evidence_dir.glob("*.json"))
    assert secret not in persisted
    assert len(fake.calls) == 1


def test_json_unicode_escaped_credential_is_suppressed_before_persistence(
    tmp_path: Path,
) -> None:
    secret = "synthetic-secret-marker"
    escaped_secret = "".join(
        f"\\u{ord(char):04x}" if index % 2 == 0 else char for index, char in enumerate(secret)
    )
    body = ('{"code":0,"data":{"item":[]},"echo":"' + escaped_secret + '"}').encode("ascii")
    fake = _FakeClient([FuyaoRawExchangeResponse(status_code=200, body=body, elapsed_seconds=0.01)])

    receipt, _auth_path, evidence_dir = _run(tmp_path, fake, key=secret)

    assert receipt["failure_code"] == "credential_reflection_detected"
    page_receipt = receipt["page_receipts"][0]  # type: ignore[index]
    assert page_receipt["response_suppressed"] is True
    assert page_receipt["raw_response_persisted"] is False
    assert not (evidence_dir / "raw").exists()


def test_invalid_utf8_body_is_not_persisted_when_reflection_check_cannot_decode(
    tmp_path: Path,
) -> None:
    body = b'{"code":0,"data":{"item":[]},"echo":"\xff"}'
    fake = _FakeClient([FuyaoRawExchangeResponse(status_code=200, body=body, elapsed_seconds=0.01)])

    receipt, _auth_path, evidence_dir = _run(tmp_path, fake)

    assert receipt["failure_code"] == "credential_reflection_check_failed"
    page_receipt = receipt["page_receipts"][0]  # type: ignore[index]
    assert page_receipt["response_suppressed"] is True
    assert page_receipt["raw_response_persisted"] is False
    assert not (evidence_dir / "raw").exists()


@pytest.mark.parametrize(
    "encode",
    [
        lambda value: value.encode(),
        lambda value: quote(value, safe="").encode(),
        lambda value: base64.b64encode(value.encode()),
        lambda value: value.encode().hex().encode(),
    ],
)
def test_credential_reflection_detects_common_encodings(
    encode: Callable[[str], bytes],
) -> None:
    secret = "synthetic-secret-marker"
    encoded = encode(secret)

    assert capture_module._contains_credential_reflection(encoded, secret) is True


def test_non_200_body_is_reflection_checked_then_retained_as_failed_raw_evidence(
    tmp_path: Path,
) -> None:
    fake = _FakeClient([_response([{"error": "synthetic"}], status=503)])

    receipt, _auth_path, evidence_dir = _run(tmp_path, fake)

    assert receipt["outcome"] == "partial_or_failed_capture"
    assert receipt["failure_code"] == "http_status_not_200"
    assert receipt["row_count"] == 0
    assert len(fake.calls) == 1
    page_receipt = receipt["page_receipts"][0]  # type: ignore[index]
    assert page_receipt["raw_response_persisted"] is True
    assert page_receipt["http_status"] == 503
    assert any((evidence_dir / "raw").rglob("*.blob"))


def test_parser_rejection_keeps_raw_body_and_stops_pagination(tmp_path: Path) -> None:
    malformed = FuyaoRawExchangeResponse(status_code=200, body=b"{", elapsed_seconds=0.01)
    fake = _FakeClient([malformed, _response([{"ticker": "must-not-request"}])])

    receipt, _auth_path, evidence_dir = _run(tmp_path, fake)

    assert receipt["outcome"] == "partial_or_failed_capture"
    assert receipt["failure_code"] == "offline_parser_rejected"
    assert receipt["row_count"] == 0
    assert len(fake.calls) == 1
    page_receipt = receipt["page_receipts"][0]  # type: ignore[index]
    assert page_receipt["raw_response_persisted"] is True
    assert page_receipt["failure_code"] == "offline_parser_rejected"
    assert any((evidence_dir / "raw").rglob("*.blob"))


def test_transport_failure_keeps_reservation_and_sanitizes_exception(tmp_path: Path) -> None:
    marker = "exception-secret-marker"
    fake = _FakeClient([RuntimeError(marker)])

    receipt, _auth_path, evidence_dir = _run(tmp_path, fake)

    assert receipt["outcome"] == "partial_or_failed_capture"
    assert receipt["failure_code"] == "request_failed_or_outcome_unknown"
    assert len(fake.calls) == 1
    assert (evidence_dir / "page-01-reservation.json").exists()
    assert (evidence_dir / "page-01-receipt.json").exists()
    assert not (evidence_dir / "raw").exists()
    persisted = "".join(path.read_text(encoding="utf-8") for path in evidence_dir.glob("*.json"))
    assert marker not in persisted


def test_scope_mismatch_rejects_before_client_call_and_keeps_auth_file(tmp_path: Path) -> None:
    fake = _FakeClient([_response([])])

    receipt, auth_path, evidence_dir = _run(
        tmp_path,
        fake,
        auth_overrides={"page_limit": capture_module.PAGE_LIMIT - 1},
    )

    assert receipt["outcome"] == "partial_or_failed_capture"
    assert receipt["failure_code"] == "authorization_scope_mismatch"
    assert receipt["authorization_state"] == "rejected"
    assert fake.calls == []
    assert auth_path.exists()
    assert (evidence_dir / "receipt.json").exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_total_response_bytes", capture_module.MAX_TOTAL_RESPONSE_BYTES - 1),
        ("max_task_rows", capture_module.MAX_TASK_ROWS - 1),
        ("max_task_timeout_seconds", capture_module.MAX_TASK_TIMEOUT_SECONDS - 1.0),
    ],
)
def test_authorization_must_match_every_task_level_cap(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    fake = _FakeClient([_response([])])

    receipt, auth_path, evidence_dir = _run(
        tmp_path,
        fake,
        auth_overrides={field: value},
    )

    assert receipt["failure_code"] == "authorization_scope_mismatch"
    assert receipt["authorization_state"] == "rejected"
    assert fake.calls == []
    assert auth_path.exists()
    assert (evidence_dir / "receipt.json").exists()


def test_noncanonical_origin_is_rejected_before_client_factory(tmp_path: Path) -> None:
    auth_path = _authorization(tmp_path / "authorization.json")
    evidence_dir = tmp_path / "evidence"
    credentials = FuyaoCredentials("synthetic-test-key", base_url="https://example.invalid")
    unused_fake = _FakeClient([])

    receipt = capture_module.run_catalog_capture(
        auth_path,
        evidence_dir,
        credentials=credentials,
        client_factory=lambda _credentials: unused_fake,
        authorization_state_dir=tmp_path / "nonce-state",
    )

    assert receipt["outcome"] == "partial_or_failed_capture"
    assert receipt["failure_code"] == "provider_origin_not_allowed"
    assert unused_fake.calls == []
    assert (evidence_dir / "run-reservation.json").exists()


def test_evidence_directory_is_never_reused_or_overwritten(tmp_path: Path) -> None:
    evidence_dir = tmp_path / "already-existing"
    evidence_dir.mkdir(mode=0o700)
    sentinel = evidence_dir / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")

    with pytest.raises(capture_module.CatalogCaptureError, match="evidence_directory_must_be_new"):
        capture_module.run_catalog_capture(
            _authorization(tmp_path / "unused-authorization.json"),
            evidence_dir,
            credentials=FuyaoCredentials("synthetic-test-key"),
            client_factory=lambda _credentials: _FakeClient([]),
            authorization_state_dir=tmp_path / "nonce-state",
        )

    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_raw_and_summary_paths_do_not_follow_symlinks(tmp_path: Path) -> None:
    parent = tmp_path / "real-parent"
    parent.mkdir(mode=0o700)
    link = tmp_path / "linked-parent"
    link.symlink_to(parent, target_is_directory=True)
    fake = _FakeClient([_response([])])
    credentials = FuyaoCredentials("synthetic-test-key")

    with pytest.raises(capture_module.CatalogCaptureError, match="private_directory_unavailable"):
        capture_module.run_catalog_capture(
            _authorization(tmp_path / "authorization.json"),
            link / "evidence",
            credentials=credentials,
            client_factory=lambda _credentials: fake,
            authorization_state_dir=tmp_path / "nonce-state",
        )

    assert fake.calls == []


def test_authorization_json_duplicate_keys_is_rejected_without_sending(tmp_path: Path) -> None:
    auth_path = tmp_path / "authorization.json"
    auth_path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
    os.chmod(auth_path, 0o600)
    fake = _FakeClient([_response([])])
    evidence_dir = tmp_path / "evidence"

    receipt = capture_module.run_catalog_capture(
        auth_path,
        evidence_dir,
        credentials=FuyaoCredentials("synthetic-test-key"),
        client_factory=lambda _credentials: fake,
        authorization_state_dir=tmp_path / "nonce-state",
    )

    assert receipt["failure_code"] == "authorization_json_invalid"
    assert fake.calls == []
    assert auth_path.exists()


def test_authorization_hard_link_cannot_replay_a_one_use_grant(tmp_path: Path) -> None:
    auth_path = _authorization(tmp_path / "authorization.json")
    replay_path = tmp_path / "authorization-replay.json"
    os.link(auth_path, replay_path)
    fake = _FakeClient([_response([])])
    evidence_dir = tmp_path / "evidence"

    receipt = capture_module.run_catalog_capture(
        auth_path,
        evidence_dir,
        credentials=FuyaoCredentials("synthetic-test-key"),
        client_factory=lambda _credentials: fake,
        authorization_state_dir=tmp_path / "nonce-state",
    )

    assert receipt["failure_code"] == "authorization_file_permissions_invalid"
    assert fake.calls == []
    assert auth_path.exists()
    assert replay_path.exists()


def test_nonce_state_directory_does_not_follow_symlinks(tmp_path: Path) -> None:
    auth_path = _authorization(tmp_path / "authorization.json")
    state_target = tmp_path / "real-nonce-state"
    state_target.mkdir(mode=0o700)
    state_link = tmp_path / "linked-nonce-state"
    state_link.symlink_to(state_target, target_is_directory=True)
    fake = _FakeClient([_response([])])

    receipt = capture_module.run_catalog_capture(
        auth_path,
        tmp_path / "evidence",
        credentials=FuyaoCredentials("synthetic-test-key"),
        client_factory=lambda _credentials: fake,
        authorization_state_dir=state_link,
    )

    assert receipt["failure_code"] == "authorization_nonce_state_unavailable"
    assert fake.calls == []
    assert auth_path.exists()
    assert not any(state_target.iterdir())


def test_authorization_copy_cannot_replay_a_consumed_grant(tmp_path: Path) -> None:
    auth_path = _authorization(tmp_path / "authorization.json")
    replay_path = tmp_path / "authorization-copy.json"
    replay_path.write_bytes(auth_path.read_bytes())
    os.chmod(replay_path, 0o600)
    state_dir = tmp_path / "nonce-state"
    first_fake = _FakeClient([_response([])])
    second_fake = _FakeClient([_response([])])

    first_receipt = capture_module.run_catalog_capture(
        auth_path,
        tmp_path / "evidence-first",
        credentials=FuyaoCredentials("synthetic-test-key"),
        client_factory=lambda _credentials: first_fake,
        authorization_state_dir=state_dir,
    )
    second_receipt = capture_module.run_catalog_capture(
        replay_path,
        tmp_path / "evidence-second",
        credentials=FuyaoCredentials("synthetic-test-key"),
        client_factory=lambda _credentials: second_fake,
        authorization_state_dir=state_dir,
    )

    assert first_receipt["outcome"] == "bounded_current_snapshot_sample_captured"
    assert second_receipt["failure_code"] == "authorization_replay_detected"
    assert second_fake.calls == []
    assert replay_path.exists()
    assert stat.S_IMODE(state_dir.stat().st_mode) == 0o700
    markers = tuple(state_dir.glob("nonce-*.json"))
    assert len(markers) == 1
    assert stat.S_IMODE(markers[0].stat().st_mode) == 0o600


def test_configured_page_and_task_caps_are_fixed() -> None:
    assert capture_module.PAGE_LIMIT == 10_000
    assert capture_module.MAX_PAGES == 2
    assert capture_module.MAX_REQUESTS == 2
    assert capture_module.MAX_RESPONSE_BYTES == 8 * 1024 * 1024
    assert capture_module.MAX_TOTAL_RESPONSE_BYTES == 16 * 1024 * 1024
    assert capture_module.MAX_TASK_ROWS == 20_000
    assert capture_module.REQUEST_TIMEOUT_SECONDS == 12.0
    assert capture_module.MAX_TASK_TIMEOUT_SECONDS == 30.0


def test_failure_receipt_contains_no_raw_catalog_fields() -> None:
    page = capture_module._PageResult(
        page_input=capture_module.FuyaoInstrumentPageInput(
            sequence_number=1,
            raw_response_body=None,
            declared_response_sha256=None,
            attempt_status="unknown",
        ),
        receipt={
            "failure_code": "must-not-copy",
            "response_sha256": "must-not-copy",
            "private_value": "must-not-copy",
        },
        row_count=0,
    )

    receipt = capture_module._failure_receipt(
        [page],
        failure_code="authorization_file_unavailable",
        authorization_state="accepted",
        page_capture_calls_started=2,
    )

    assert receipt["failure_code"] == "authorization_file_unavailable"
    assert receipt["row_values_in_receipt"] is False
    assert receipt["pages_attempted"] == 2
    assert receipt["pages_with_receipts"] == 1
    assert receipt["unreceipted_page_capture_calls"] == 1
    assert receipt["unreceipted_attempt_outcome"] == "unknown"
    assert receipt["page_receipts"] == [{}]
    assert "private_value" not in json.dumps(receipt)
    assert "must-not-copy" not in json.dumps(receipt)


def test_page_receipt_allowlist_validates_types_values_and_digests() -> None:
    response_digest = "a" * 64
    safe = capture_module._sanitize_page_receipt(
        {
            "page_sequence": True,
            "offset": {},
            "attempt_outcome": "unknown",
            "failure_code": "credential_reflection_detected",
            "http_status": True,
            "response_bytes": -1,
            "response_sha256": "not-a-digest",
            "raw_manifest_sha256": response_digest,
            "raw_response_persisted": 1,
            "raw_persistence_state": {"unsafe": True},
            "response_suppressed": False,
            "parsed_row_count": capture_module.PAGE_LIMIT + 1,
            "request_summary_sha256": response_digest,
            "credential_reflection": "not_detected_in_entity_body",
            "source_authenticity": "not_established",
            "page_completeness": "not_established",
            "private_value": "must-not-copy",
        }
    )

    assert safe == {
        "attempt_outcome": "unknown",
        "failure_code": "credential_reflection_detected",
        "raw_manifest_sha256": response_digest,
        "response_suppressed": False,
        "request_summary_sha256": response_digest,
        "credential_reflection": "not_detected_in_entity_body",
        "source_authenticity": "not_established",
        "page_completeness": "not_established",
    }


def test_unexpected_transport_response_type_is_recorded_without_retry(tmp_path: Path) -> None:
    class InvalidResponseClient(_FakeClient):
        def get_raw_exchange(
            self, endpoint: str, *, params: Mapping[str, object]
        ) -> FuyaoRawExchangeResponse:
            super().get_raw_exchange(endpoint, params=params)
            return cast("FuyaoRawExchangeResponse", object())

    fake = InvalidResponseClient([_response([])])
    receipt, _auth_path, evidence_dir = _run(tmp_path, fake)

    assert receipt["failure_code"] == "response_contract_invalid"
    assert len(fake.calls) == 1
    assert receipt["page_receipts"][0]["response_suppressed"] is False  # type: ignore[index]
    assert (evidence_dir / "page-01-receipt.json").exists()


def test_invalid_http_response_shape_is_rejected_before_raw_persistence(tmp_path: Path) -> None:
    invalid = FuyaoRawExchangeResponse(status_code=700, body=b"synthetic", elapsed_seconds=0.01)
    fake = _FakeClient([invalid])
    receipt, _auth_path, evidence_dir = _run(tmp_path, fake)

    assert receipt["failure_code"] == "response_contract_invalid"
    assert "http_status" not in receipt["page_receipts"][0]  # type: ignore[index]
    assert not (evidence_dir / "raw").exists()


def test_non_bytes_response_body_is_rejected_before_raw_persistence(tmp_path: Path) -> None:
    invalid = FuyaoRawExchangeResponse(
        status_code=200,
        body=cast("bytes", bytearray(b"synthetic")),
        elapsed_seconds=0.01,
    )
    fake = _FakeClient([invalid])

    receipt, _auth_path, evidence_dir = _run(tmp_path, fake)

    assert receipt["failure_code"] == "response_contract_invalid"
    assert not (evidence_dir / "raw").exists()


def test_credential_reflection_check_exception_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_check(*_args: object, **_kwargs: object) -> bool:
        raise RuntimeError("synthetic-check-failure")

    monkeypatch.setattr(capture_module, "_contains_credential_reflection", fail_check)
    fake = _FakeClient([_response([])])

    receipt, _auth_path, evidence_dir = _run(tmp_path, fake)

    assert receipt["failure_code"] == "credential_reflection_check_failed"
    assert receipt["page_receipts"][0]["response_suppressed"] is True  # type: ignore[index]
    assert not (evidence_dir / "raw").exists()


def test_raw_persistence_failure_is_anonymous_and_does_not_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = "synthetic-persistence-exception"

    def fail_capture(*_args: object, **_kwargs: object) -> object:
        raise OSError(marker)

    monkeypatch.setattr(capture_module.RawResponseArtifactWriter, "capture", fail_capture)
    fake = _FakeClient([_response([])])

    receipt, _auth_path, evidence_dir = _run(tmp_path, fake)

    assert receipt["failure_code"] == "raw_evidence_persistence_failed"
    assert len(fake.calls) == 1
    assert receipt["page_receipts"][0]["raw_persistence_state"] == "unknown_after_writer_failure"  # type: ignore[index]
    assert marker not in json.dumps(receipt)
    assert not (evidence_dir / "raw").exists()


def test_unexpected_parser_exception_keeps_safe_page_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = "synthetic-parser-exception"
    original_parser = capture_module.build_fuyao_instrument_census
    calls = 0

    def parser(
        pages: Sequence[capture_module.FuyaoInstrumentPageInput], *, observed_at: datetime
    ) -> capture_module.FuyaoInstrumentCensusCapture:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError(marker)
        return original_parser(pages, observed_at=observed_at)

    monkeypatch.setattr(capture_module, "build_fuyao_instrument_census", parser)
    fake = _FakeClient([_response([{"ticker": "synthetic"}])])

    receipt, _auth_path, evidence_dir = _run(tmp_path, fake)

    assert receipt["failure_code"] == "offline_parser_rejected"
    assert marker not in json.dumps(receipt)
    assert receipt["page_receipts"][0]["raw_response_persisted"] is True  # type: ignore[index]
    assert (evidence_dir / "page-01-receipt.json").exists()


def test_page_row_limit_is_rejected_after_preserving_safe_raw_evidence(tmp_path: Path) -> None:
    rows = [{"ticker": f"synthetic-{index}"} for index in range(capture_module.PAGE_LIMIT + 1)]
    fake = _FakeClient([_response(rows)])

    receipt, _auth_path, evidence_dir = _run(tmp_path, fake)

    assert receipt["failure_code"] == "offline_parser_rejected"
    assert len(fake.calls) == 1
    assert receipt["page_receipts"][0]["raw_response_persisted"] is True  # type: ignore[index]
    assert any((evidence_dir / "raw").rglob("*.blob"))


@pytest.mark.parametrize(
    ("cap_name", "cap_value", "rows", "failure_code"),
    [
        ("MAX_TASK_ROWS", 1, [{"ticker": "one"}, {"ticker": "two"}], "task_row_cap_exceeded"),
        ("MAX_TOTAL_RESPONSE_BYTES", 1, [{"ticker": "one"}], "task_response_byte_cap_exceeded"),
    ],
)
def test_task_caps_stop_after_one_bounded_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cap_name: str,
    cap_value: int,
    rows: list[dict[str, str]],
    failure_code: str,
) -> None:
    monkeypatch.setattr(capture_module, cap_name, cap_value)
    fake = _FakeClient([_response(rows)])

    receipt, _auth_path, _evidence_dir = _run(tmp_path, fake)

    assert receipt["failure_code"] == failure_code
    assert len(fake.calls) == 1


def test_unexpected_aggregate_failure_keeps_existing_page_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = "synthetic-aggregate-exception"

    def fail_aggregate(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError(marker)

    monkeypatch.setattr(capture_module, "_aggregate_pages", fail_aggregate)
    fake = _FakeClient([_response([{"ticker": "synthetic"}])])

    receipt, _auth_path, evidence_dir = _run(tmp_path, fake)

    assert receipt["failure_code"] == "local_capture_operation_failed"
    assert marker not in json.dumps(receipt)
    assert (evidence_dir / "page-01-receipt.json").exists()
    assert (evidence_dir / "receipt.json").exists()


def test_shared_failure_receipt_uses_generic_code_for_unknown_code() -> None:
    receipt = capture_module._failure_receipt(
        [],
        failure_code="synthetic-unrecognized-code",
        authorization_state="synthetic-unrecognized-state",
    )

    assert receipt["failure_code"] == "local_capture_operation_failed"
    assert receipt["authorization_state"] == "not_accepted"
    assert "synthetic-unrecognized" not in json.dumps(receipt)


def test_success_summary_omits_invalid_digest_values() -> None:
    response = _response([])
    params = {"asset_type": "a-share", "limit": capture_module.PAGE_LIMIT, "offset": 0}
    request_summary = capture_module._request_summary(0, params)
    page = capture_module._PageResult(
        page_input=capture_module._page_input(
            1,
            0,
            200,
            response.body,
            hashlib.sha256(response.body).hexdigest(),
            request_summary,
            succeeded=True,
        ),
        receipt={
            "response_sha256": "must-not-copy",
            "raw_manifest_sha256": "A" * 64,
        },
        row_count=0,
    )
    aggregate = capture_module._aggregate_pages([page])

    receipt = capture_module._success_receipt(aggregate, [page])

    assert receipt["response_sha256s"] == []
    assert receipt["raw_manifest_sha256s"] == []
    assert "must-not-copy" not in json.dumps(receipt)


def test_live_and_offline_environment_gates_are_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("TESTING", raising=False)
    with pytest.raises(
        capture_module.CatalogCaptureError, match="explicit_development_environment_required"
    ):
        capture_module._require_live_environment()

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("TESTING", " YeS ")
    with pytest.raises(
        capture_module.CatalogCaptureError,
        match="catalog_capture_disabled_in_testing_environment",
    ):
        capture_module._require_live_environment()
    monkeypatch.setenv("TESTING", "false")
    capture_module._require_live_environment()

    with pytest.raises(
        capture_module.CatalogCaptureError,
        match="offline_injection_requires_testing_environment",
    ):
        capture_module._require_offline_fake_environment()
    monkeypatch.setenv("APP_ENV", "testing")
    with pytest.raises(
        capture_module.CatalogCaptureError,
        match="offline_injection_requires_testing_flag",
    ):
        capture_module._require_offline_fake_environment()
    monkeypatch.setenv("TESTING", "true")
    capture_module._require_offline_fake_environment()


def test_cli_prints_only_returned_receipt_and_sanitizes_rejection(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        capture_module,
        "run_catalog_capture",
        lambda *_args: {"outcome": "bounded_current_snapshot_sample_captured"},
    )
    args = ["--authorization-file", "/tmp/auth.json", "--evidence-dir", "/tmp/evidence"]

    assert capture_module.main(args) == 0
    assert json.loads(capsys.readouterr().out) == {
        "outcome": "bounded_current_snapshot_sample_captured"
    }

    def reject(*_args: object) -> dict[str, object]:
        raise capture_module.CatalogCaptureError("authorization_file_unavailable")

    monkeypatch.setattr(capture_module, "run_catalog_capture", reject)
    assert capture_module.main(args) == 1
    assert json.loads(capsys.readouterr().out) == {
        "code": "authorization_file_unavailable",
        "outcome": "rejected",
    }

    marker = "synthetic-private-exception-detail"

    def reject_with_unknown_code(*_args: object) -> dict[str, object]:
        raise capture_module.CatalogCaptureError(marker)

    monkeypatch.setattr(capture_module, "run_catalog_capture", reject_with_unknown_code)
    assert capture_module.main(args) == 1
    rejected_output = capsys.readouterr().out
    assert json.loads(rejected_output) == {
        "code": "local_capture_operation_failed",
        "outcome": "rejected",
    }
    assert marker not in rejected_output

    monkeypatch.setattr(
        capture_module,
        "run_catalog_capture",
        lambda *_args: {"outcome": "partial_or_failed_capture"},
    )
    assert capture_module.main(args) == 1
    assert json.loads(capsys.readouterr().out) == {"outcome": "partial_or_failed_capture"}


def test_credentials_are_resolved_lazily_without_reading_environment_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = FuyaoCredentials("synthetic-provider-key")
    fake_provider = SimpleNamespace(credentials=lambda: expected)
    original_import = builtins.__import__

    def import_provider(name: str, *args: object, **kwargs: object) -> object:
        if name == "opendata.data.providers.ths.models._client":
            return fake_provider
        return original_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", import_provider)

    assert capture_module._load_project_credentials() is expected


def test_provider_credential_import_failure_is_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_import = builtins.__import__

    def fail_provider(name: str, *args: object, **kwargs: object) -> object:
        if name == "opendata.data.providers.ths.models._client":
            raise ImportError("synthetic-private-import-detail")
        return original_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", fail_provider)

    with pytest.raises(
        capture_module.CatalogCaptureError, match="provider_credentials_unavailable"
    ) as caught:
        capture_module._load_project_credentials()
    assert "synthetic-private-import-detail" not in str(caught.value)


def test_provider_credentials_must_be_exact_project_credential_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_provider = SimpleNamespace(credentials=lambda: object())
    original_import = builtins.__import__

    def import_provider(name: str, *args: object, **kwargs: object) -> object:
        if name == "opendata.data.providers.ths.models._client":
            return fake_provider
        return original_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", import_provider)
    with pytest.raises(capture_module.CatalogCaptureError, match="provider_credentials_invalid"):
        capture_module._load_project_credentials()


def test_make_client_enforces_fake_contract_and_fixed_live_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credentials = FuyaoCredentials("synthetic-client-key")
    with pytest.raises(capture_module.CatalogCaptureError, match="client_contract_invalid"):
        capture_module._make_client(
            credentials, lambda _credentials: cast("capture_module._RawCatalogClient", object())
        )

    built: dict[str, object] = {}

    class FakeTransport:
        def __init__(self, **kwargs: object) -> None:
            built.update(kwargs)

        def get_raw_exchange(self, _endpoint: str, **_kwargs: object) -> FuyaoRawExchangeResponse:
            raise AssertionError("transport construction test must not send")

        def close(self) -> None:
            return None

    monkeypatch.setattr(capture_module, "FuyaoHttpClient", FakeTransport)

    client = capture_module._make_client(credentials, None)

    assert isinstance(client, FakeTransport)
    assert built["credentials"] is credentials
    assert built["max_attempts"] == 1
    assert built["max_response_bytes"] == capture_module.MAX_RESPONSE_BYTES
    assert built["timeout_seconds"] == capture_module.REQUEST_TIMEOUT_SECONDS
    assert built["raw_response_cache"] is None


@pytest.mark.parametrize(
    "encode",
    [
        lambda secret: json.dumps(json.dumps(secret, ensure_ascii=True)).encode("ascii"),
        lambda secret: html.escape(html.escape(secret, quote=True), quote=True).encode(),
        lambda secret: quote(quote(secret, safe=""), safe="").encode(),
    ],
)
def test_reflection_guard_checks_bounded_nested_encodings(
    encode: Callable[[str], bytes],
) -> None:
    secret = "synthetic <secret> & marker 😀"

    assert capture_module._contains_credential_reflection(encode(secret), secret) is True


def test_reflection_guard_rejects_missing_key_or_non_byte_body() -> None:
    with pytest.raises(
        capture_module.CatalogCaptureError, match="credential_reflection_check_unavailable"
    ):
        capture_module._contains_credential_reflection(b"safe", "")
    with pytest.raises(
        capture_module.CatalogCaptureError, match="credential_reflection_check_unavailable"
    ):
        capture_module._contains_credential_reflection(cast("bytes", bytearray(b"safe")), "key")


def test_authorization_validation_rejects_nonce_and_time_scope(
    tmp_path: Path,
) -> None:
    auth_path = _authorization(tmp_path / "authorization.json")
    base_payload = json.loads(auth_path.read_text(encoding="utf-8"))

    bad_nonce = dict(base_payload, nonce="invalid")
    with pytest.raises(capture_module.CatalogCaptureError, match="authorization_nonce_invalid"):
        capture_module._validate_authorization(
            bad_nonce, json.dumps(bad_nonce, sort_keys=True).encode()
        )

    bad_timestamp = dict(base_payload, issued_at_utc="not-a-canonical-date")
    with pytest.raises(capture_module.CatalogCaptureError, match="authorization_timestamp_invalid"):
        capture_module._validate_authorization(
            bad_timestamp, json.dumps(bad_timestamp, sort_keys=True).encode()
        )

    future = capture_module._utcnow() + timedelta(minutes=1)
    future_payload = dict(
        base_payload,
        issued_at_utc=capture_module._format_utc(future),
        expires_at_utc=capture_module._format_utc(future + timedelta(minutes=1)),
    )
    with pytest.raises(
        capture_module.CatalogCaptureError, match="authorization_expired_or_not_yet_valid"
    ):
        capture_module._validate_authorization(
            future_payload, json.dumps(future_payload, sort_keys=True).encode()
        )

    expired = capture_module._utcnow() - timedelta(minutes=1)
    expired_payload = dict(
        base_payload,
        issued_at_utc=capture_module._format_utc(expired - timedelta(minutes=1)),
        expires_at_utc=capture_module._format_utc(expired),
    )
    with pytest.raises(
        capture_module.CatalogCaptureError, match="authorization_expired_or_not_yet_valid"
    ):
        capture_module._validate_authorization(
            expired_payload, json.dumps(expired_payload, sort_keys=True).encode()
        )

    missing_field = dict(base_payload)
    missing_field.pop("nonce")
    with pytest.raises(capture_module.CatalogCaptureError, match="authorization_fields_invalid"):
        capture_module._validate_authorization(
            missing_field, json.dumps(missing_field, sort_keys=True).encode()
        )


def test_bounded_authorization_read_and_json_root_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(capture_module.CatalogCaptureError, match="authorization_root_invalid"):
        capture_module._strict_json_object(b"[]")
    with pytest.raises(capture_module.CatalogCaptureError, match="authorization_json_invalid"):
        capture_module._strict_json_object(b'{"value":NaN}')

    path = tmp_path / "oversized.json"
    path.write_bytes(b"12345")
    descriptor = os.open(path, os.O_RDONLY)
    try:
        with pytest.raises(
            capture_module.CatalogCaptureError, match="authorization_file_too_large"
        ):
            capture_module._read_bounded(descriptor, 4)
    finally:
        os.close(descriptor)


def test_private_directory_helpers_reject_relative_and_public_paths(tmp_path: Path) -> None:
    with pytest.raises(
        capture_module.CatalogCaptureError, match="absolute_private_directory_required"
    ):
        capture_module._open_private_directory_chain(Path("relative"))

    public_dir = tmp_path / "public"
    public_dir.mkdir(mode=0o700)
    os.chmod(public_dir, 0o755)
    with pytest.raises(
        capture_module.CatalogCaptureError, match="private_directory_permissions_required"
    ):
        capture_module._open_private_directory_chain(public_dir)

    with pytest.raises(
        capture_module.CatalogCaptureError, match="absolute_evidence_directory_required"
    ):
        capture_module._create_private_evidence_directory(Path("relative/evidence"))


def test_nonce_state_final_directory_must_remain_private(tmp_path: Path) -> None:
    state_path = tmp_path / "unsafe-state"
    state_path.mkdir(mode=0o700)
    os.chmod(state_path, 0o755)

    with pytest.raises(
        capture_module.CatalogCaptureError,
        match="authorization_nonce_state_permissions_invalid",
    ):
        capture_module._open_or_create_private_directory_chain(state_path)


def test_default_nonce_state_root_uses_account_home_and_rejects_relative_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        capture_module.pwd, "getpwuid", lambda _uid: SimpleNamespace(pw_dir="/home/synthetic")
    )

    assert capture_module._default_authorization_state_directory() == Path(
        "/home/synthetic/.local/state/opendata/fuyao-a-share-catalog/used-nonces"
    )

    monkeypatch.setattr(
        capture_module.pwd, "getpwuid", lambda _uid: SimpleNamespace(pw_dir="relative")
    )
    with pytest.raises(
        capture_module.CatalogCaptureError, match="authorization_nonce_state_unavailable"
    ):
        capture_module._default_authorization_state_directory()


def test_timestamp_parser_rejects_calendar_overflow_and_noncanonical_format() -> None:
    with pytest.raises(capture_module.CatalogCaptureError, match="authorization_timestamp_invalid"):
        capture_module._parse_utc_timestamp("2026-99-01T00:00:00Z")
    with pytest.raises(capture_module.CatalogCaptureError, match="authorization_timestamp_invalid"):
        capture_module._parse_utc_timestamp("2026-01-01T00:00:00.0000000Z")


def test_missing_authorization_file_is_recorded_without_client_call(tmp_path: Path) -> None:
    fake = _FakeClient([_response([])])
    receipt = capture_module.run_catalog_capture(
        tmp_path / "missing-authorization.json",
        tmp_path / "evidence",
        credentials=FuyaoCredentials("synthetic-test-key"),
        client_factory=lambda _credentials: fake,
        authorization_state_dir=tmp_path / "nonce-state",
    )

    assert receipt["failure_code"] == "authorization_file_unavailable"
    assert fake.calls == []
    assert (tmp_path / "evidence" / "receipt.json").exists()


def test_write_helpers_never_overwrite_and_fail_closed_on_short_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        capture_module._write_json_exclusive(directory_fd, "first.json", {"safe": True})
        with pytest.raises(FileExistsError):
            capture_module._write_json_exclusive(directory_fd, "first.json", {"safe": False})

        monkeypatch.setattr(capture_module.os, "write", lambda *_args: 0)
        with pytest.raises(OSError, match="short write"):
            capture_module._write_json_exclusive(directory_fd, "short.json", {"safe": True})
    finally:
        os.close(directory_fd)

    assert json.loads((tmp_path / "first.json").read_text(encoding="utf-8")) == {"safe": True}


@pytest.mark.parametrize(
    "write_error",
    [
        OSError("synthetic-write-failure"),
        capture_module.CatalogCaptureError("evidence_file_permissions_invalid"),
    ],
)
def test_write_if_missing_swallows_only_expected_write_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    write_error: Exception,
) -> None:
    directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)

    def fail_write(*_args: object, **_kwargs: object) -> None:
        raise write_error

    monkeypatch.setattr(capture_module, "_write_json_exclusive", fail_write)
    try:
        capture_module._write_if_missing(directory_fd, "receipt.json", {"safe": True})
    finally:
        os.close(directory_fd)

    assert not (tmp_path / "receipt.json").exists()


def test_authorization_expiry_is_rechecked_before_each_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expired = capture_module._Authorization(
        digest="a" * 64,
        expires_at=capture_module._utcnow() - timedelta(seconds=1),
        nonce="0" * 32,
    )

    with pytest.raises(
        capture_module.CatalogCaptureError, match="authorization_expired_before_request"
    ):
        capture_module._require_authorization_live(expired)
