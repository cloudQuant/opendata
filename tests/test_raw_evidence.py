"""Behavioral tests for bounded raw-response evidence storage."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

import opendata.pipeline.raw_evidence as raw_evidence
from opendata.pipeline.raw_evidence import (
    ArtifactIntegrityError,
    RawEvidenceError,
    RawResponseArtifactWriter,
    RawResponseManifest,
    RawResponseSource,
    ResponseTooLargeError,
    UnsafeArtifactRootError,
    request_summary_sha256,
)

if TYPE_CHECKING:
    from collections.abc import Iterable


def _source(**overrides: str) -> RawResponseSource:
    """Build one safe source identity for isolated artifact tests."""
    values = {
        "provider_id": "openbb_cn",
        "dataset_id": "equity_daily",
        "operation_id": "historical_bars",
    }
    values.update(overrides)
    return RawResponseSource(**values)


def _request_hash(symbol: str = "600000.SH") -> str:
    """Build a stock request digest from the explicit business-parameter allowlist."""
    return request_summary_sha256(
        {"symbol": symbol, "start_date": date(2024, 1, 1), "adjustment": "none"},
        allowed_fields={"symbol", "start_date", "adjustment"},
    )


def _capture(
    writer: RawResponseArtifactWriter,
    chunks: Iterable[bytes] = (b'{"bars":[', b"1,2,3]}"),
    *,
    request_hash: str | None = None,
    source: RawResponseSource | None = None,
):
    """Capture a small deterministic fixture with explicit unknown policy labels."""
    return writer.capture(
        iter(chunks),
        source=source or _source(),
        request_summary_sha256=request_hash or _request_hash(),
        http_status=200,
        retention_class="unknown",
        source_history_limit="unknown",
    )


def test_request_summary_hash_is_canonical_allowlisted_and_never_persisted() -> None:
    first = request_summary_sha256(
        {"symbol": "ABC", "start_date": date(2024, 1, 1)},
        allowed_fields={"start_date", "symbol"},
    )
    second = request_summary_sha256(
        {"start_date": date(2024, 1, 1), "symbol": "ABC"},
        allowed_fields={"symbol", "start_date"},
    )
    assert first == second
    assert len(first) == 64
    assert "ABC" not in first
    assert len(request_summary_sha256({"author": "issuer"}, allowed_fields={"author"})) == 64

    with pytest.raises(RawEvidenceError, match="allowlist"):
        request_summary_sha256({"symbol": "ABC", "limit": 10}, allowed_fields={"symbol"})
    with pytest.raises(RawEvidenceError, match="credential-like"):
        request_summary_sha256({"api_key": "never-write-me"}, allowed_fields={"api_key"})
    with pytest.raises(RawEvidenceError, match="credential-like"):
        request_summary_sha256({"symbol": "ABC"}, allowed_fields={"symbol", "auth_token"})
    with pytest.raises(RawEvidenceError, match="non-finite"):
        request_summary_sha256({"ratio": float("nan")}, allowed_fields={"ratio"})
    with pytest.raises(RawEvidenceError, match="datetime"):
        request_summary_sha256(
            {"as_of": datetime(2024, 1, 1, tzinfo=timezone.utc)}, allowed_fields={"as_of"}
        )
    with pytest.raises(RawEvidenceError, match="scalar"):
        request_summary_sha256(
            {"filters": {"symbol": "ABC"}},  # type: ignore[dict-item]
            allowed_fields={"filters"},
        )
    with pytest.raises(RawEvidenceError, match="invalid field"):
        request_summary_sha256({"Bad-Key": "value"}, allowed_fields={"Bad-Key"})
    with pytest.raises(RawEvidenceError, match="invalid field"):
        request_summary_sha256({}, allowed_fields={"Bad-Key"})
    with pytest.raises(RawEvidenceError, match="credential-like"):
        request_summary_sha256({"api_token": "sensitive"}, allowed_fields={"api_token"})
    with pytest.raises(RawEvidenceError, match="credential-like"):
        request_summary_sha256({"api_token": "sensitive"}, allowed_fields={"symbol"})
    with pytest.raises(RawEvidenceError, match="parameter name is invalid"):
        request_summary_sha256({"Bad-Key": "value"}, allowed_fields=set())
    with pytest.raises(RawEvidenceError, match="bounded summary"):
        request_summary_sha256({"symbol": "x" * 1025}, allowed_fields={"symbol"})
    assert len(request_summary_sha256({"ratio": 1.25}, allowed_fields={"ratio"})) == 64
    oversized_parameters = {f"p{index}": "x" for index in range(4000)}
    with pytest.raises(RawEvidenceError, match="size limit"):
        request_summary_sha256(oversized_parameters, allowed_fields=oversized_parameters)


def test_capture_streams_to_private_content_and_manifest_paths(tmp_path: Path) -> None:
    root = tmp_path / "raw-evidence"
    writer = RawResponseArtifactWriter(root, max_response_bytes=128)
    body = b'{"bars":[1,2,3]}'
    before = datetime.now(timezone.utc)
    captured = _capture(writer, (body[:8], body[8:]))
    after = datetime.now(timezone.utc)

    assert captured.artifact_path.is_file()
    assert captured.artifact_path.read_bytes() == body
    assert captured.manifest_path.is_file()
    assert writer.manifest_path(captured.manifest) == captured.manifest_path
    assert captured.manifest.response_sha256 == hashlib.sha256(body).hexdigest()
    assert captured.manifest.response_size_bytes == len(body)
    assert captured.manifest.captured_at_utc.tzinfo == timezone.utc
    assert before <= captured.manifest.captured_at_utc <= after
    assert captured.manifest.retention_class == "unknown"
    assert captured.manifest.source_history_limit == "unknown"

    manifest_bytes = captured.manifest_path.read_bytes()
    manifest_json = json.loads(manifest_bytes)
    assert manifest_json["request_summary_sha256"] == _request_hash()
    assert manifest_json["response_sha256"] == hashlib.sha256(body).hexdigest()
    assert "600000.SH" not in manifest_bytes.decode()
    assert "never-write-me" not in manifest_bytes.decode()
    assert stat.S_IMODE(captured.artifact_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(captured.manifest_path.stat().st_mode) == 0o600
    for directory in (
        root,
        root / "objects",
        root / "objects" / "pending",
        captured.artifact_path.parent,
        root / "manifests",
        captured.manifest_path.parent,
    ):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_equal_payload_keeps_independent_manifest_metadata(tmp_path: Path) -> None:
    writer = RawResponseArtifactWriter(tmp_path / "artifacts")
    first = _capture(writer, request_hash=_request_hash("600000.SH"))
    second = writer.capture(
        iter((b'{"bars":[1,2,3]}',)),
        source=_source(operation_id="daily_retry"),
        request_summary_sha256=_request_hash("000001.SZ"),
        http_status=206,
        retention_class="restricted",
        source_history_limit="snapshot_only",
    )

    assert first.artifact_path == second.artifact_path
    assert first.manifest_path != second.manifest_path
    assert first.manifest.capture_id != second.manifest.capture_id
    assert first.manifest.response_sha256 == second.manifest.response_sha256
    assert first.manifest.request_summary_sha256 != second.manifest.request_summary_sha256
    assert first.manifest.source.operation_id != second.manifest.source.operation_id
    assert second.manifest.http_status == 206
    assert second.manifest.retention_class == "restricted"
    assert second.manifest.source_history_limit == "snapshot_only"
    assert len(list((tmp_path / "artifacts" / "manifests").rglob("*.json"))) == 2


def test_concurrent_writers_reuse_only_verified_same_content(tmp_path: Path) -> None:
    writer = RawResponseArtifactWriter(tmp_path / "concurrent")
    body = (b"0123456789abcdef" * 4096) + b"done"

    def capture_one(_: int):
        return writer.capture(
            iter((body[:7000], body[7000:])),
            source=_source(),
            request_summary_sha256=_request_hash(),
            http_status=200,
            retention_class="unknown",
            source_history_limit="unknown",
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(capture_one, range(16)))

    assert len({result.artifact_path for result in results}) == 1
    assert results[0].artifact_path.read_bytes() == body
    assert len({result.manifest_path for result in results}) == len(results)
    assert all(result.manifest_path.is_file() for result in results)


def test_interrupted_stream_removes_temporary_content_and_writes_no_manifest(
    tmp_path: Path,
) -> None:
    writer = RawResponseArtifactWriter(tmp_path / "interrupted")

    def interrupted_chunks():
        yield b"partial body"
        raise RuntimeError("upstream stream interrupted")

    with pytest.raises(RuntimeError, match="interrupted"):
        writer.capture(
            interrupted_chunks(),
            source=_source(),
            request_summary_sha256=_request_hash(),
            http_status=200,
            retention_class="restricted",
            source_history_limit="unknown",
        )

    pending = tmp_path / "interrupted" / "objects" / "pending"
    assert list(pending.iterdir()) == []
    assert not list((tmp_path / "interrupted").rglob("*.blob"))
    assert not list((tmp_path / "interrupted").rglob("*.json"))


def test_response_limit_is_enforced_before_excess_chunk_is_written(tmp_path: Path) -> None:
    writer = RawResponseArtifactWriter(tmp_path / "bounded", max_response_bytes=8)
    observed: list[str] = []

    def chunks():
        yield b"12345678"
        observed.append("next-chunk-requested")
        yield b"9"

    with pytest.raises(ResponseTooLargeError):
        _capture(writer, chunks=chunks())

    assert observed == ["next-chunk-requested"]
    pending = tmp_path / "bounded" / "objects" / "pending"
    assert list(pending.iterdir()) == []
    assert not list((tmp_path / "bounded").rglob("*.blob"))


def test_corrupted_content_addressed_file_is_rejected(tmp_path: Path) -> None:
    writer = RawResponseArtifactWriter(tmp_path / "corrupt")
    captured = _capture(writer)
    captured.artifact_path.write_bytes(b"tampered")

    with pytest.raises(ArtifactIntegrityError, match="failed hash or size"):
        _capture(writer)

    assert captured.artifact_path.read_bytes() == b"tampered"
    assert list((tmp_path / "corrupt" / "objects" / "pending").iterdir()) == []


def test_existing_content_addressed_file_with_wrong_mode_is_rejected(tmp_path: Path) -> None:
    writer = RawResponseArtifactWriter(tmp_path / "wrong-mode")
    captured = _capture(writer)
    captured.artifact_path.chmod(0o644)
    with pytest.raises(ArtifactIntegrityError, match="private regular file"):
        _capture(writer)


def test_symlinked_root_or_managed_directory_cannot_escape(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    symlink_root = tmp_path / "root-link"
    symlink_root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(UnsafeArtifactRootError):
        _capture(RawResponseArtifactWriter(symlink_root))

    managed_root = tmp_path / "managed-root"
    managed_root.mkdir(mode=0o700)
    (managed_root / "objects").symlink_to(outside, target_is_directory=True)
    with pytest.raises(UnsafeArtifactRootError):
        _capture(RawResponseArtifactWriter(managed_root))
    assert list(outside.iterdir()) == []


def test_invalid_manifest_timestamps_and_capture_metadata_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        RawResponseManifest(
            source=_source(),
            request_summary_sha256=_request_hash(),
            captured_at_utc=datetime(2024, 1, 1),
            http_status=200,
            response_sha256="0" * 64,
            response_size_bytes=0,
            retention_class="unknown",
            source_history_limit="unknown",
        )
    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        RawResponseManifest(
            source=_source(),
            request_summary_sha256=_request_hash(),
            captured_at_utc=datetime(2024, 1, 1, tzinfo=timezone(timedelta(hours=2))),
            http_status=200,
            response_sha256="0" * 64,
            response_size_bytes=0,
            retention_class="unknown",
            source_history_limit="unknown",
        )
    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        RawResponseManifest(
            source=_source(),
            request_summary_sha256=_request_hash(),
            captured_at_utc=datetime(2024, 1, 1, tzinfo=timezone.utc).replace(tzinfo=None),
            http_status=200,
            response_sha256="0" * 64,
            response_size_bytes=0,
            retention_class="unknown",
            source_history_limit="unknown",
        )

    writer = RawResponseArtifactWriter(tmp_path / "bad-metadata")
    with pytest.raises(RawEvidenceError, match="declared classification"):
        writer.capture(
            iter((b"body",)),
            source=_source(),
            request_summary_sha256=_request_hash(),
            http_status=200,
            retention_class="licensed",  # type: ignore[arg-type]
            source_history_limit="unknown",
        )
    with pytest.raises(RawEvidenceError, match="declared finite status"):
        writer.capture(
            iter((b"body",)),
            source=_source(),
            request_summary_sha256=_request_hash(),
            http_status=200,
            retention_class="unknown",
            source_history_limit="complete",  # type: ignore[arg-type]
        )
    assert not (tmp_path / "bad-metadata").exists()


def test_writer_rejects_relative_and_parent_traversal_roots(tmp_path: Path) -> None:
    with pytest.raises(UnsafeArtifactRootError):
        RawResponseArtifactWriter(Path("relative-artifacts"))
    with pytest.raises(UnsafeArtifactRootError):
        RawResponseArtifactWriter(tmp_path / ".." / "escaped")


def test_capture_input_validation_happens_before_root_creation(tmp_path: Path) -> None:
    with pytest.raises(RawEvidenceError, match="max_response_bytes must be an integer"):
        RawResponseArtifactWriter(tmp_path / "bool-limit", max_response_bytes=True)
    with pytest.raises(RawEvidenceError, match="finite range"):
        RawResponseArtifactWriter(tmp_path / "zero-limit", max_response_bytes=0)

    root = tmp_path / "invalid-capture"
    writer = RawResponseArtifactWriter(root)
    assert writer.artifact_root == root
    with pytest.raises(RawEvidenceError, match="validated RawResponseSource"):
        writer.capture(
            iter((b"body",)),
            source="provider",  # type: ignore[arg-type]
            request_summary_sha256=_request_hash(),
            http_status=200,
            retention_class="unknown",
            source_history_limit="unknown",
        )
    for request_hash, status, expected_error in (
        ("BAD", 200, "lowercase SHA-256"),
        (_request_hash(), True, "HTTP status"),
        (_request_hash(), 99, "HTTP status"),
    ):
        with pytest.raises(RawEvidenceError, match=expected_error):
            writer.capture(
                iter((b"body",)),
                source=_source(),
                request_summary_sha256=request_hash,
                http_status=status,
                retention_class="unknown",
                source_history_limit="unknown",
            )  # type: ignore[arg-type]
    with pytest.raises(RawEvidenceError, match="validated RawResponseManifest"):
        writer.manifest_path("not a manifest")  # type: ignore[arg-type]
    assert not root.exists()


def test_unsafe_existing_root_modes_and_missing_no_follow_support_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_root = tmp_path / "root-file"
    file_root.write_text("not a directory")
    with pytest.raises(UnsafeArtifactRootError, match="unsafe or unreadable"):
        _capture(RawResponseArtifactWriter(file_root))

    permissive_root = tmp_path / "permissive-root"
    permissive_root.mkdir(mode=0o700)
    permissive_root.chmod(0o755)
    with pytest.raises(UnsafeArtifactRootError, match="must be private"):
        _capture(RawResponseArtifactWriter(permissive_root))

    monkeypatch.delattr(raw_evidence.os, "O_NOFOLLOW")
    with pytest.raises(UnsafeArtifactRootError, match="cannot guarantee"):
        _capture(RawResponseArtifactWriter(tmp_path / "unsupported-platform"))


def test_wrong_managed_directory_mode_and_non_bytes_are_rejected_and_cleaned(
    tmp_path: Path,
) -> None:
    root = tmp_path / "unsafe-directory"
    root.mkdir(mode=0o700)
    (root / "objects").mkdir(mode=0o700)
    (root / "objects" / "pending").mkdir(mode=0o755)
    with pytest.raises(UnsafeArtifactRootError, match="mode 0700"):
        _capture(RawResponseArtifactWriter(root))

    writer = RawResponseArtifactWriter(tmp_path / "non-bytes")
    with pytest.raises(RawEvidenceError, match="chunks must be bytes"):
        writer.capture(
            iter(("sensitive response",)),  # type: ignore[arg-type]
            source=_source(),
            request_summary_sha256=_request_hash(),
            http_status=200,
            retention_class="unknown",
            source_history_limit="unknown",
        )
    assert list((tmp_path / "non-bytes" / "objects" / "pending").iterdir()) == []


def test_existing_symlink_and_oversized_content_objects_are_rejected(tmp_path: Path) -> None:
    writer = RawResponseArtifactWriter(tmp_path / "existing-symlink", max_response_bytes=8)
    captured = writer.capture(
        iter((b"12345678",)),
        source=_source(),
        request_summary_sha256=_request_hash(),
        http_status=200,
        retention_class="unknown",
        source_history_limit="unknown",
    )
    external = tmp_path / "external-content"
    external.write_bytes(b"secret")
    captured.artifact_path.unlink()
    captured.artifact_path.symlink_to(external)
    with pytest.raises(ArtifactIntegrityError, match="cannot be opened safely"):
        writer.capture(
            iter((b"12345678",)),
            source=_source(),
            request_summary_sha256=_request_hash(),
            http_status=200,
            retention_class="unknown",
            source_history_limit="unknown",
        )
    assert external.read_bytes() == b"secret"

    captured.artifact_path.unlink()
    captured.artifact_path.write_bytes(b"123456789")
    captured.artifact_path.chmod(0o600)
    with pytest.raises(ArtifactIntegrityError, match="exceeds its configured size"):
        writer.capture(
            iter((b"12345678",)),
            source=_source(),
            request_summary_sha256=_request_hash(),
            http_status=200,
            retention_class="unknown",
            source_history_limit="unknown",
        )


def test_manifest_no_clobber_and_write_failure_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = RawResponseArtifactWriter(tmp_path / "manifest-retry")
    captured = _capture(writer)
    manifest_hash = hashlib.sha256(captured.manifest_path.read_bytes()).hexdigest()
    assert captured.manifest_path.name == f"{manifest_hash}.json"

    def fail_write(*_: object, **__: object) -> int:
        raise OSError("simulated local storage failure")

    manifest_directory_fd = os.open(
        captured.manifest_path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    )
    try:
        duplicate_content = b"same metadata bytes"
        duplicate_hash = hashlib.sha256(duplicate_content).hexdigest()
        raw_evidence._store_bytes_no_clobber(
            manifest_directory_fd,
            "duplicate.json",
            duplicate_content,
            expected_hash=duplicate_hash,
        )
        raw_evidence._store_bytes_no_clobber(
            manifest_directory_fd,
            "duplicate.json",
            duplicate_content,
            expected_hash=duplicate_hash,
        )
        assert (captured.manifest_path.parent / "duplicate.json").read_bytes() == duplicate_content

        monkeypatch.setattr(raw_evidence.os, "write", fail_write)
        with pytest.raises(OSError, match="simulated"):
            raw_evidence._store_bytes_no_clobber(
                manifest_directory_fd,
                "new-manifest.json",
                b"new manifest",
                expected_hash=hashlib.sha256(b"new manifest").hexdigest(),
            )
    finally:
        os.close(manifest_directory_fd)
    assert not (captured.manifest_path.parent / "new-manifest.json").exists()
    assert not list(captured.manifest_path.parent.glob(".tmp-*"))


def test_zero_length_os_write_aborts_and_cleans_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = RawResponseArtifactWriter(tmp_path / "zero-write")
    monkeypatch.setattr(raw_evidence.os, "write", lambda *_: 0)
    with pytest.raises(OSError, match="short write"):
        _capture(writer)
    assert list((tmp_path / "zero-write" / "objects" / "pending").iterdir()) == []
