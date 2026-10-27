"""Behavioral checks for content-addressed raw evidence resolution."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path

import pytest

import opendata.pipeline.raw_evidence as raw_evidence
import opendata.pipeline.raw_evidence_reader as evidence_reader
from opendata.pipeline.raw_evidence import (
    RawResponseArtifactWriter,
    RawResponseSource,
    request_summary_sha256,
)
from opendata.pipeline.raw_evidence_reader import (
    RawEvidenceContextMismatchError,
    RawEvidenceIntegrityError,
    RawEvidenceReadInputError,
    RawResponseArtifactReader,
    UnsafeRawEvidenceError,
)

_FAR_FUTURE_UTC = datetime(2099, 1, 1, tzinfo=timezone.utc)


def _source(**overrides: str) -> RawResponseSource:
    """Build the fixed test source identity used by the writer and reader."""
    values = {
        "provider_id": "ths_fuyao",
        "dataset_id": "financial_statement",
        "operation_id": "income_statement",
    }
    values.update(overrides)
    return RawResponseSource(**values)


def _request_digest(symbol: str = "600000.SH") -> str:
    """Return a source request digest from a small explicit parameter allowlist."""
    return request_summary_sha256(
        {"symbol": symbol, "report_period": date(2024, 12, 31)},
        allowed_fields={"symbol", "report_period"},
    )


def _capture(root: Path, body: bytes = b'{"income":[]}'):
    """Use the production writer to create one private content-addressed fixture."""
    return RawResponseArtifactWriter(root).capture(
        iter((body,)),
        source=_source(),
        request_summary_sha256=_request_digest(),
        http_status=200,
        retention_class="unknown",
        source_history_limit="unknown",
    )


def _store_manifest_bytes(root: Path, content: bytes) -> str:
    """Address a synthetic manifest payload in the writer's private directory layout."""
    digest = hashlib.sha256(content).hexdigest()
    prefix = root / "manifests" / digest[:2]
    prefix.mkdir(mode=0o700, parents=True, exist_ok=True)
    prefix.chmod(0o700)
    path = prefix / f"{digest}.json"
    path.write_bytes(content)
    path.chmod(0o600)
    return digest


def _reader(root: Path, *, maximum: int = 1024) -> RawResponseArtifactReader:
    """Bind a reader to an explicit fixture directory and finite response limit."""
    return RawResponseArtifactReader(root, max_response_bytes=maximum)


def _read(
    root: Path,
    manifest_sha256: str,
    *,
    captured_at_not_after: datetime,
    source: RawResponseSource | None = None,
):
    """Resolve one fixture using its fixed expected source and request digest."""
    return _reader(root).read(
        manifest_sha256,
        expected_source=source or _source(),
        expected_request_summary_sha256=_request_digest(),
        captured_at_not_after=captured_at_not_after,
    )


def test_writer_capture_round_trips_exact_manifest_and_original_bytes(tmp_path: Path) -> None:
    root = tmp_path / "private-evidence"
    body = b"provider-private-fixture-content"
    capture = _capture(root, body)

    result = _read(
        root,
        capture.manifest_path.stem,
        captured_at_not_after=capture.manifest.captured_at_utc,
    )

    assert result.body == body
    assert result.manifest == capture.manifest
    assert result.manifest_sha256 == capture.manifest_path.stem
    assert result.manifest.response_sha256 == hashlib.sha256(body).hexdigest()
    assert result.manifest.response_size_bytes == len(body)
    assert body.decode() not in repr(result)
    assert stat.S_IMODE(capture.manifest_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(capture.artifact_path.stat().st_mode) == 0o600


def test_empty_response_is_returned_only_with_matching_empty_body_digest(tmp_path: Path) -> None:
    root = tmp_path / "empty-evidence"
    capture = _capture(root, b"")

    result = _read(
        root,
        capture.manifest_path.stem,
        captured_at_not_after=capture.manifest.captured_at_utc,
    )

    assert result.body == b""
    assert result.manifest.response_size_bytes == 0
    assert result.manifest.response_sha256 == hashlib.sha256(b"").hexdigest()


def test_invalid_digest_and_nonabsolute_root_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(RawEvidenceReadInputError, match="explicit absolute path"):
        RawResponseArtifactReader(42)
    with pytest.raises(RawEvidenceReadInputError, match="absolute"):
        RawResponseArtifactReader(Path("relative"))
    with pytest.raises(RawEvidenceReadInputError, match="absolute"):
        RawResponseArtifactReader(tmp_path / ".." / "outside")
    with pytest.raises(RawEvidenceReadInputError, match="finite supported limit"):
        RawResponseArtifactReader(tmp_path / "too-large", max_response_bytes=64 * 1024 * 1024 + 1)
    with pytest.raises(RawEvidenceReadInputError, match="must be an integer"):
        RawResponseArtifactReader(tmp_path / "boolean-limit", max_response_bytes=True)
    with pytest.raises(RawEvidenceReadInputError, match="lowercase SHA-256"):
        _read(
            tmp_path / "does-not-exist",
            "../" + "a" * 62,
            captured_at_not_after=_FAR_FUTURE_UTC,
        )


def test_manifest_and_request_context_must_match_exactly(tmp_path: Path) -> None:
    root = tmp_path / "context-evidence"
    capture = _capture(root)
    reader = _reader(root)

    with pytest.raises(RawEvidenceContextMismatchError, match="source"):
        reader.read(
            capture.manifest_path.stem,
            expected_source=_source(provider_id="another_provider"),
            expected_request_summary_sha256=_request_digest(),
            captured_at_not_after=capture.manifest.captured_at_utc,
        )
    with pytest.raises(RawEvidenceContextMismatchError, match="request digest"):
        reader.read(
            capture.manifest_path.stem,
            expected_source=_source(),
            expected_request_summary_sha256=_request_digest("000001.SZ"),
            captured_at_not_after=capture.manifest.captured_at_utc,
        )
    with pytest.raises(RawEvidenceContextMismatchError, match="HTTP status"):
        reader.read(
            capture.manifest_path.stem,
            expected_source=_source(),
            expected_request_summary_sha256=_request_digest(),
            captured_at_not_after=capture.manifest.captured_at_utc,
            expected_http_status=206,
        )
    with pytest.raises(RawEvidenceReadInputError, match="HTTP status code"):
        reader.read(
            capture.manifest_path.stem,
            expected_source=_source(),
            expected_request_summary_sha256=_request_digest(),
            captured_at_not_after=capture.manifest.captured_at_utc,
            expected_http_status=True,
        )


def test_caller_source_must_be_exactly_validated(tmp_path: Path) -> None:
    root = tmp_path / "invalid-source-evidence"
    capture = _capture(root)
    reader = _reader(root)

    class SpecializedSource(RawResponseSource):
        pass

    with pytest.raises(RawEvidenceReadInputError, match="validated RawResponseSource"):
        reader.read(
            capture.manifest_path.stem,
            expected_source=SpecializedSource(**_source().model_dump()),
            expected_request_summary_sha256=_request_digest(),
            captured_at_not_after=capture.manifest.captured_at_utc,
        )

    malformed_source = RawResponseSource.model_construct(
        provider_id="invalid source id",
        dataset_id="financial_statement",
        operation_id="income_statement",
    )
    with pytest.raises(RawEvidenceReadInputError, match="expected_source failed validation"):
        reader.read(
            capture.manifest_path.stem,
            expected_source=malformed_source,
            expected_request_summary_sha256=_request_digest(),
            captured_at_not_after=capture.manifest.captured_at_utc,
        )


def test_capture_time_uses_a_required_utc_bound_with_inclusive_boundary(
    tmp_path: Path,
) -> None:
    root = tmp_path / "capture-time-evidence"
    capture = _capture(root)
    reader = _reader(root)
    bound = capture.manifest.captured_at_utc

    result = reader.read(
        capture.manifest_path.stem,
        expected_source=_source(),
        expected_request_summary_sha256=_request_digest(),
        captured_at_not_after=bound,
    )
    assert result.manifest.captured_at_utc == bound

    with pytest.raises(RawEvidenceContextMismatchError, match="after the trusted UTC bound"):
        reader.read(
            capture.manifest_path.stem,
            expected_source=_source(),
            expected_request_summary_sha256=_request_digest(),
            captured_at_not_after=bound - timedelta(microseconds=1),
        )
    with pytest.raises(RawEvidenceReadInputError, match="aware UTC"):
        reader.read(
            capture.manifest_path.stem,
            expected_source=_source(),
            expected_request_summary_sha256=_request_digest(),
            captured_at_not_after=bound.replace(tzinfo=None),
        )
    with pytest.raises(RawEvidenceReadInputError, match="UTC offset zero"):
        reader.read(
            capture.manifest_path.stem,
            expected_source=_source(),
            expected_request_summary_sha256=_request_digest(),
            captured_at_not_after=bound.astimezone(timezone(timedelta(hours=1))),
        )

    class BrokenTimezone(tzinfo):
        def utcoffset(self, dt: datetime | None) -> timedelta | None:
            del dt
            raise ValueError("timezone offset is unavailable")

        def dst(self, dt: datetime | None) -> timedelta | None:
            del dt
            return None

        def tzname(self, dt: datetime | None) -> str:
            del dt
            return "broken"

    broken_bound = datetime(2026, 1, 1, tzinfo=BrokenTimezone())
    with pytest.raises(RawEvidenceReadInputError, match="aware UTC"):
        reader.read(
            capture.manifest_path.stem,
            expected_source=_source(),
            expected_request_summary_sha256=_request_digest(),
            captured_at_not_after=broken_bound,
        )


def test_future_manifest_capture_time_requires_an_explicit_future_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    actual_datetime = raw_evidence.datetime

    class FutureCaptureDateTime(actual_datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            return actual_datetime.now(tz) + timedelta(minutes=1)

    monkeypatch.setattr(raw_evidence, "datetime", FutureCaptureDateTime)
    root = tmp_path / "future-capture-evidence"
    capture = _capture(root)
    actual_bound = actual_datetime.now(timezone.utc)
    reader = _reader(root)

    with pytest.raises(RawEvidenceContextMismatchError, match="after the trusted UTC bound"):
        reader.read(
            capture.manifest_path.stem,
            expected_source=_source(),
            expected_request_summary_sha256=_request_digest(),
            captured_at_not_after=actual_bound,
        )

    exact_future_bound = capture.manifest.captured_at_utc
    result = reader.read(
        capture.manifest_path.stem,
        expected_source=_source(),
        expected_request_summary_sha256=_request_digest(),
        captured_at_not_after=exact_future_bound,
    )
    assert result.manifest.captured_at_utc == exact_future_bound


def test_modified_manifest_or_body_bytes_fail_their_addressed_hash(tmp_path: Path) -> None:
    root = tmp_path / "tampered-evidence"
    capture = _capture(root, b"a fixed length body")
    capture.manifest_path.write_bytes(capture.manifest_path.read_bytes() + b" ")

    with pytest.raises(RawEvidenceIntegrityError, match="manifest bytes"):
        _read(
            root,
            capture.manifest_path.stem,
            captured_at_not_after=capture.manifest.captured_at_utc,
        )

    other_root = tmp_path / "tampered-body"
    other = _capture(other_root, b"a fixed length body")
    other.artifact_path.write_bytes(b"z fixed length body")
    with pytest.raises(RawEvidenceIntegrityError, match="response bytes"):
        _read(
            other_root,
            other.manifest_path.stem,
            captured_at_not_after=other.manifest.captured_at_utc,
        )


def test_manifest_requires_writer_canonical_json_even_at_its_own_digest(tmp_path: Path) -> None:
    root = tmp_path / "noncanonical-evidence"
    capture = _capture(root)
    original = capture.manifest_path.read_bytes()
    altered = original + b" "
    digest = hashlib.sha256(altered).hexdigest()
    prefix = root / "manifests" / digest[:2]
    prefix.mkdir(mode=0o700, parents=True, exist_ok=True)
    prefix.chmod(0o700)
    path = prefix / f"{digest}.json"
    path.write_bytes(altered)
    path.chmod(0o600)

    with pytest.raises(RawEvidenceIntegrityError, match="canonical"):
        _read(root, digest, captured_at_not_after=capture.manifest.captured_at_utc)


@pytest.mark.parametrize(
    ("content", "message"),
    [(b"[]", "ambiguous"), (b"{", "invalid"), (b"{}", "invalid")],
)
def test_nonobject_malformed_and_schema_invalid_manifests_fail_closed(
    tmp_path: Path, content: bytes, message: str
) -> None:
    root = tmp_path / "invalid-json-evidence"
    capture = _capture(root)
    digest = _store_manifest_bytes(root, content)

    with pytest.raises(RawEvidenceIntegrityError, match=message):
        _read(root, digest, captured_at_not_after=capture.manifest.captured_at_utc)


def test_manifest_cannot_relabel_the_body_length(tmp_path: Path) -> None:
    root = tmp_path / "misstated-size-evidence"
    body = b"verified bytes"
    capture = _capture(root, body)
    relabelled = capture.manifest.model_copy(update={"response_size_bytes": len(body) + 1})
    manifest_bytes = json.dumps(
        relabelled.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = _store_manifest_bytes(root, manifest_bytes)

    with pytest.raises(RawEvidenceIntegrityError, match="manifest size"):
        _read(root, digest, captured_at_not_after=capture.manifest.captured_at_utc)


def test_symlinked_managed_file_is_not_followed(tmp_path: Path) -> None:
    root = tmp_path / "symlink-evidence"
    capture = _capture(root)
    outside = tmp_path / "outside.blob"
    outside.write_bytes(capture.artifact_path.read_bytes())
    capture.artifact_path.unlink()
    capture.artifact_path.symlink_to(outside)

    with pytest.raises(UnsafeRawEvidenceError, match="unavailable or unsafe"):
        _read(
            root,
            capture.manifest_path.stem,
            captured_at_not_after=capture.manifest.captured_at_utc,
        )


def test_symlinked_nested_object_directory_is_not_followed(tmp_path: Path) -> None:
    root = tmp_path / "nested-symlink-evidence"
    capture = _capture(root)
    object_prefix = capture.artifact_path.parent
    detached_prefix = tmp_path / "detached-object-prefix"
    os.replace(object_prefix, detached_prefix)
    object_prefix.symlink_to(detached_prefix, target_is_directory=True)

    with pytest.raises(UnsafeRawEvidenceError, match="managed evidence directory is unavailable"):
        _read(
            root,
            capture.manifest_path.stem,
            captured_at_not_after=capture.manifest.captured_at_utc,
        )


def test_hardlink_is_allowed_but_mutation_through_same_uid_alias_fails_hash(
    tmp_path: Path,
) -> None:
    root = tmp_path / "hardlink-evidence"
    capture = _capture(root, b"stable response bytes")
    alias = tmp_path / "same-user-alias.blob"
    os.link(capture.artifact_path, alias)

    accepted = _read(
        root,
        capture.manifest_path.stem,
        captured_at_not_after=capture.manifest.captured_at_utc,
    )
    assert accepted.body == b"stable response bytes"

    alias.write_bytes(b"Xtable response bytes")
    with pytest.raises(RawEvidenceIntegrityError, match="response bytes"):
        _read(
            root,
            capture.manifest_path.stem,
            captured_at_not_after=capture.manifest.captured_at_utc,
        )


def test_directory_at_body_address_is_not_read_as_a_file(tmp_path: Path) -> None:
    root = tmp_path / "nonfile-evidence"
    capture = _capture(root)
    capture.artifact_path.unlink()
    capture.artifact_path.mkdir(mode=0o700)

    with pytest.raises(UnsafeRawEvidenceError, match="regular file"):
        _read(
            root,
            capture.manifest_path.stem,
            captured_at_not_after=capture.manifest.captured_at_utc,
        )


def test_reader_fails_closed_when_required_nofollow_flag_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "unsupported-platform-evidence"
    capture = _capture(root)
    monkeypatch.delattr(evidence_reader.os, "O_NOFOLLOW")

    with pytest.raises(UnsafeRawEvidenceError, match="platform cannot safely verify"):
        _read(
            root,
            capture.manifest_path.stem,
            captured_at_not_after=capture.manifest.captured_at_utc,
        )


def test_manifest_unlink_during_read_fails_as_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "unlink-race-evidence"
    capture = _capture(root)
    real_read = evidence_reader.os.read
    removed = False

    def unlink_manifest_while_open(descriptor: int, size: int) -> bytes:
        nonlocal removed
        if not removed:
            removed = True
            capture.manifest_path.unlink()
        return real_read(descriptor, size)

    monkeypatch.setattr(evidence_reader.os, "read", unlink_manifest_while_open)
    with pytest.raises(UnsafeRawEvidenceError, match="became unavailable"):
        _read(
            root,
            capture.manifest_path.stem,
            captured_at_not_after=capture.manifest.captured_at_utc,
        )
    assert removed


def test_response_growth_during_read_stays_within_the_byte_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "grow-race-evidence"
    capture = _capture(root, b"tiny")
    body_inode = capture.artifact_path.stat().st_ino
    real_read = evidence_reader.os.read
    grew = False

    def grow_body_before_read(descriptor: int, size: int) -> bytes:
        nonlocal grew
        if not grew and os.fstat(descriptor).st_ino == body_inode:
            grew = True
            with capture.artifact_path.open("r+b") as stream:
                stream.seek(0)
                stream.write(b"x" * 9)
                stream.truncate(9)
        return real_read(descriptor, size)

    monkeypatch.setattr(evidence_reader.os, "read", grow_body_before_read)
    with pytest.raises(UnsafeRawEvidenceError, match="byte limit"):
        _reader(root, maximum=8).read(
            capture.manifest_path.stem,
            expected_source=_source(),
            expected_request_summary_sha256=_request_digest(),
            captured_at_not_after=capture.manifest.captured_at_utc,
        )
    assert grew


def test_symlinked_root_and_wrong_owner_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    actual_root = tmp_path / "actual-evidence"
    capture = _capture(actual_root)
    linked_root = tmp_path / "linked-evidence"
    linked_root.symlink_to(actual_root, target_is_directory=True)
    with pytest.raises(UnsafeRawEvidenceError, match="root is unavailable or unsafe"):
        _read(
            linked_root,
            capture.manifest_path.stem,
            captured_at_not_after=capture.manifest.captured_at_utc,
        )

    original_geteuid = os.geteuid
    monkeypatch.setattr(evidence_reader.os, "geteuid", lambda: original_geteuid() + 1)
    with pytest.raises(UnsafeRawEvidenceError, match="owned by the current user"):
        _read(
            actual_root,
            capture.manifest_path.stem,
            captured_at_not_after=capture.manifest.captured_at_utc,
        )


@pytest.mark.parametrize(
    "target", ["root", "manifest_directory", "manifest_file", "body_directory"]
)
def test_nonprivate_modes_are_rejected(tmp_path: Path, target: str) -> None:
    root = tmp_path / f"mode-{target}"
    capture = _capture(root)
    paths = {
        "root": root,
        "manifest_directory": capture.manifest_path.parent,
        "manifest_file": capture.manifest_path,
        "body_directory": capture.artifact_path.parent,
    }
    path = paths[target]
    path.chmod(0o755 if path.is_dir() else 0o644)

    with pytest.raises(UnsafeRawEvidenceError, match="mode"):
        _read(
            root,
            capture.manifest_path.stem,
            captured_at_not_after=capture.manifest.captured_at_utc,
        )


def test_reader_rejects_manifest_and_body_above_configured_limits(tmp_path: Path) -> None:
    root = tmp_path / "size-evidence"
    capture = _capture(root, b"1234567890")

    with pytest.raises(UnsafeRawEvidenceError, match="reader limit"):
        _reader(root, maximum=9).read(
            capture.manifest_path.stem,
            expected_source=_source(),
            expected_request_summary_sha256=_request_digest(),
            captured_at_not_after=capture.manifest.captured_at_utc,
        )

    body_root = tmp_path / "oversize-body"
    body_capture = _capture(body_root, b"1234")
    body_capture.artifact_path.write_bytes(b"123456789")
    with pytest.raises(UnsafeRawEvidenceError, match="byte limit"):
        _reader(body_root, maximum=8).read(
            body_capture.manifest_path.stem,
            expected_source=_source(),
            expected_request_summary_sha256=_request_digest(),
            captured_at_not_after=body_capture.manifest.captured_at_utc,
        )


def test_duplicate_manifest_keys_are_rejected_when_readdressed(tmp_path: Path) -> None:
    root = tmp_path / "duplicate-json"
    capture = _capture(root)
    original = capture.manifest_path.read_bytes()
    payload = json.loads(original)
    duplicated = original[:-1] + b',"http_status":200}'
    assert payload["http_status"] == 200
    digest = hashlib.sha256(duplicated).hexdigest()
    prefix = root / "manifests" / digest[:2]
    prefix.mkdir(mode=0o700, parents=True, exist_ok=True)
    prefix.chmod(0o700)
    path = prefix / f"{digest}.json"
    path.write_bytes(duplicated)
    path.chmod(0o600)

    with pytest.raises(RawEvidenceIntegrityError, match="ambiguous"):
        _read(root, digest, captured_at_not_after=capture.manifest.captured_at_utc)


def test_file_replacement_during_read_is_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "racing-evidence"
    capture = _capture(root)
    real_read = evidence_reader.os.read
    changed = False

    def replace_manifest_while_open(descriptor: int, size: int) -> bytes:
        nonlocal changed
        if not changed:
            changed = True
            with capture.manifest_path.open("r+b") as stream:
                stream.seek(0)
                stream.write(b" " * len(capture.manifest_path.read_bytes()))
                stream.flush()
        return real_read(descriptor, size)

    monkeypatch.setattr(evidence_reader.os, "read", replace_manifest_while_open)
    with pytest.raises((UnsafeRawEvidenceError, RawEvidenceIntegrityError)):
        _read(
            root,
            capture.manifest_path.stem,
            captured_at_not_after=capture.manifest.captured_at_utc,
        )
    assert changed
