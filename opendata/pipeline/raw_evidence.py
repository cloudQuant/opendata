"""Store bounded, content-addressed copies of upstream HTTP response bodies.

This module performs no network, database, or environment access. Its manifest is
operational provenance only: retention labels and source-history limits are
caller-supplied metadata and do not establish permission, licensing, or approval
to retain source content.
"""

from __future__ import annotations

import hashlib
import json

# Keep the helper's former module dependencies visible for callers that imported
# names such as ``math``, ``Mapping``, or ``date`` from this module directly or via
# ``import *``.
import math as math
import os
import re
import secrets
import stat

# Keep this runtime import: ``Iterable`` appears in public annotations that callers
# may resolve with ``typing.get_type_hints``.
from collections.abc import Iterable  # noqa: TC003

# Explicit same-name aliases retain these legacy module exports after extraction.
from collections.abc import Mapping as Mapping
from dataclasses import dataclass
from datetime import date as date
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
)

# Retain the legacy request-summary exports while keeping digest users out of the
# pipeline package's application-settings and database initialization path.
from opendata.data.request_summary import (
    RawEvidenceError,
)
from opendata.data.request_summary import (
    RequestValue as RequestValue,
)
from opendata.data.request_summary import (
    request_summary_sha256 as request_summary_sha256,
)

RetentionClass: TypeAlias = Literal["unknown", "ephemeral", "restricted", "durable_candidate"]
HistoryLimit: TypeAlias = Literal[
    "unknown", "provider_retention_window", "snapshot_only", "partial_history", "none_documented"
]
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
_MAX_MANIFEST_BYTES = 16 * 1024
_DEFAULT_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_MAX_CONFIGURED_RESPONSE_BYTES = 1024 * 1024 * 1024


class ResponseTooLargeError(RawEvidenceError):
    """Raised before a response body can exceed the configured byte limit."""


class ArtifactIntegrityError(RawEvidenceError):
    """Raised when an existing content-addressed artifact fails verification."""


class UnsafeArtifactRootError(RawEvidenceError):
    """Raised when the artifact root or a managed directory is unsafe."""


class RawResponseSource(BaseModel):
    """Safe source identity fields; URLs, credentials, and free-form headers are excluded."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    provider_id: StrictStr = Field(pattern=_IDENTIFIER_PATTERN)
    dataset_id: StrictStr = Field(pattern=_IDENTIFIER_PATTERN)
    operation_id: StrictStr = Field(pattern=_IDENTIFIER_PATTERN)


class RawResponseManifest(BaseModel):
    """Immutable metadata for one captured response body."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    manifest_version: Literal["1"] = "1"
    capture_id: StrictStr = Field(
        default_factory=lambda: secrets.token_hex(16), pattern=r"^[0-9a-f]{32}$"
    )
    source: RawResponseSource
    # Same identifier as the helper re-export; this is a distinct manifest field.
    request_summary_sha256: StrictStr = Field(pattern=_SHA256_PATTERN)  # noqa: F811
    captured_at_utc: datetime
    http_status: StrictInt = Field(ge=100, le=599)
    response_sha256: StrictStr = Field(pattern=_SHA256_PATTERN)
    response_size_bytes: StrictInt = Field(ge=0, le=_MAX_CONFIGURED_RESPONSE_BYTES)
    retention_class: RetentionClass
    source_history_limit: HistoryLimit

    @field_validator("captured_at_utc")
    @classmethod
    def require_utc_datetime(cls, value: datetime) -> datetime:
        """Reject naive and non-UTC collection timestamps."""
        offset = value.utcoffset()
        if value.tzinfo is None or offset is None or offset.total_seconds() != 0:
            raise ValueError("captured_at_utc must be timezone-aware UTC")
        return value


@dataclass(frozen=True, slots=True)
class CapturedRawResponse:
    """Returned manifest and local paths for one successfully persisted response."""

    manifest: RawResponseManifest
    artifact_path: Path
    manifest_path: Path


class RawResponseArtifactWriter:
    """Persist streamed response chunks below an explicit private local root."""

    def __init__(
        self, artifact_root: Path | str, *, max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES
    ) -> None:
        """Create a writer with an explicit root and a finite response size limit."""
        root = Path(artifact_root)
        if not root.is_absolute() or ".." in root.parts or len(root.parts) < 2:
            raise UnsafeArtifactRootError(
                "artifact_root must be a non-root absolute path without '..'"
            )
        if not isinstance(max_response_bytes, int) or isinstance(max_response_bytes, bool):
            raise RawEvidenceError("max_response_bytes must be an integer")
        if not 1 <= max_response_bytes <= _MAX_CONFIGURED_RESPONSE_BYTES:
            raise RawEvidenceError("max_response_bytes is outside the supported finite range")
        self._artifact_root = root
        self._max_response_bytes = max_response_bytes

    @property
    def artifact_root(self) -> Path:
        """Return the configured absolute artifact root."""
        return self._artifact_root

    def capture(
        self,
        chunks: Iterable[bytes],
        *,
        source: RawResponseSource,
        # Preserve the established capture argument name alongside the helper export.
        request_summary_sha256: str,  # noqa: F811
        http_status: int,
        retention_class: RetentionClass,
        source_history_limit: HistoryLimit,
    ) -> CapturedRawResponse:
        """Stream, hash, atomically persist, and describe one upstream response.

        The body is never included in logs or returned values. A repeated body is
        reused only after checking its exact size and SHA-256; metadata is addressed
        independently by its own hash, so captures never overwrite one another.
        """
        source = _revalidate_source(source)
        if (
            not isinstance(request_summary_sha256, str)
            or re.fullmatch(_SHA256_PATTERN, request_summary_sha256) is None
        ):
            raise RawEvidenceError("request_summary_sha256 must be lowercase SHA-256 hex")
        if (
            not isinstance(http_status, int)
            or isinstance(http_status, bool)
            or not 100 <= http_status <= 599
        ):
            raise RawEvidenceError("http_status must be an HTTP status code")
        if retention_class not in ("unknown", "ephemeral", "restricted", "durable_candidate"):
            raise RawEvidenceError("retention_class must use a declared classification")
        if source_history_limit not in (
            "unknown",
            "provider_retention_window",
            "snapshot_only",
            "partial_history",
            "none_documented",
        ):
            raise RawEvidenceError("source_history_limit must use a declared finite status")

        root_fd = _open_root_directory(self._artifact_root)
        try:
            object_directory_fd = _open_private_child(root_fd, "objects")
            try:
                object_prefix_fd = _open_private_child(object_directory_fd, "pending")
                response_name: str | None = None
                try:
                    response_hash, response_size, response_name = _store_chunks(
                        object_prefix_fd, chunks, self._max_response_bytes
                    )
                    # Content is moved to its digest directory only after the stream
                    # is complete and bounded; pending is never a manifest path.
                    response_prefix = response_hash[:2]
                    response_prefix_fd = _open_private_child(object_directory_fd, response_prefix)
                    try:
                        artifact_name = f"{response_hash}.blob"
                        _publish_existing_temp(
                            source_directory_fd=object_prefix_fd,
                            destination_fd=response_prefix_fd,
                            temporary_name=response_name,
                            final_name=artifact_name,
                            expected_hash=response_hash,
                            expected_size=response_size,
                            maximum_size=self._max_response_bytes,
                        )
                        response_name = None
                    finally:
                        os.close(response_prefix_fd)
                finally:
                    if response_name is not None:
                        _unlink_if_present(object_prefix_fd, response_name)
                    os.close(object_prefix_fd)
            finally:
                os.close(object_directory_fd)

            manifest = RawResponseManifest(
                source=source,
                request_summary_sha256=request_summary_sha256,
                captured_at_utc=datetime.now(timezone.utc),
                http_status=http_status,
                response_sha256=response_hash,
                response_size_bytes=response_size,
                retention_class=retention_class,
                source_history_limit=source_history_limit,
            )
            manifest_bytes = _canonical_manifest_bytes(manifest)
            manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
            manifest_directory_fd = _open_private_child(root_fd, "manifests")
            try:
                manifest_prefix_fd = _open_private_child(manifest_directory_fd, manifest_hash[:2])
                try:
                    manifest_name = f"{manifest_hash}.json"
                    _store_bytes_no_clobber(
                        manifest_prefix_fd,
                        manifest_name,
                        manifest_bytes,
                        expected_hash=manifest_hash,
                    )
                finally:
                    os.close(manifest_prefix_fd)
            finally:
                os.close(manifest_directory_fd)
        finally:
            os.close(root_fd)

        return CapturedRawResponse(
            manifest=manifest,
            artifact_path=self._artifact_root
            / "objects"
            / response_hash[:2]
            / f"{response_hash}.blob",
            manifest_path=self._artifact_root
            / "manifests"
            / manifest_hash[:2]
            / f"{manifest_hash}.json",
        )

    def manifest_path(self, manifest: RawResponseManifest) -> Path:
        """Reconstruct the content-addressed local path for a manifest."""
        manifest_hash = hashlib.sha256(_canonical_manifest_bytes(manifest)).hexdigest()
        return self._artifact_root / "manifests" / manifest_hash[:2] / f"{manifest_hash}.json"


def _canonical_manifest_bytes(manifest: RawResponseManifest) -> bytes:
    """Return deterministic JSON bytes for a validated manifest."""
    validated = _revalidate_manifest(manifest)
    return json.dumps(
        validated.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _revalidate_source(source: object) -> RawResponseSource:
    """Deep-dump and validate a source model at the persistence boundary."""
    if not isinstance(source, RawResponseSource):
        raise RawEvidenceError("source must be a validated RawResponseSource")
    try:
        source_dump = source.model_dump(mode="python")
        return RawResponseSource.model_validate(source_dump, strict=True)
    except ValidationError as exc:
        raise RawEvidenceError("source identity failed boundary validation") from exc


def _revalidate_manifest(manifest: object) -> RawResponseManifest:
    """Deep-dump and validate a manifest and its nested source at the boundary."""
    if not isinstance(manifest, RawResponseManifest):
        raise RawEvidenceError("manifest must be a validated RawResponseManifest")
    try:
        payload = manifest.model_dump(mode="python")
        source_dump = payload.get("source")
        payload["source"] = RawResponseSource.model_validate(source_dump, strict=True)
        return RawResponseManifest.model_validate(payload, strict=True)
    except ValidationError as exc:
        raise RawEvidenceError("manifest failed boundary validation") from exc


def _open_root_directory(path: Path) -> int:
    """Open or create an absolute directory tree while refusing symlink traversal."""
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise UnsafeArtifactRootError("this platform cannot guarantee no-follow directory access")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    parts = path.parts[1:]
    try:
        for index, component in enumerate(parts):
            if component in {"", ".", ".."}:
                raise UnsafeArtifactRootError("artifact root contains an unsafe path component")
            is_target = index == len(parts) - 1
            created = False
            try:
                os.mkdir(component, mode=0o700, dir_fd=descriptor)
                created = True
            except FileExistsError:
                pass
            child_descriptor = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            info = os.fstat(child_descriptor)
            if not stat.S_ISDIR(info.st_mode):
                os.close(child_descriptor)
                raise UnsafeArtifactRootError("artifact root component is not a directory")
            if created:
                os.fchmod(child_descriptor, 0o700)
                os.fsync(child_descriptor)
                os.fsync(descriptor)
            elif is_target and stat.S_IMODE(info.st_mode) & 0o077:
                os.close(child_descriptor)
                raise UnsafeArtifactRootError("existing artifact root must be private to its owner")
            os.close(descriptor)
            descriptor = child_descriptor
        return descriptor
    except UnsafeArtifactRootError:
        os.close(descriptor)
        raise
    except OSError as exc:
        os.close(descriptor)
        raise UnsafeArtifactRootError(
            "artifact root contains an unsafe or unreadable component"
        ) from exc
    except BaseException:
        os.close(descriptor)
        raise


def _open_private_child(parent_descriptor: int, name: str) -> int:
    """Open a generated child directory and enforce private permissions."""
    if not re.fullmatch(r"[a-z0-9]{1,64}", name):
        raise UnsafeArtifactRootError("generated artifact directory name is invalid")
    created = False
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent_descriptor)
        created = True
    except FileExistsError:
        pass
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent_descriptor,
        )
    except OSError as exc:
        raise UnsafeArtifactRootError("artifact directory is not a safe real directory") from exc
    info = os.fstat(descriptor)
    if not stat.S_ISDIR(info.st_mode):
        os.close(descriptor)
        raise UnsafeArtifactRootError("artifact path component is not a directory")
    if created:
        os.fchmod(descriptor, 0o700)
        os.fsync(descriptor)
        os.fsync(parent_descriptor)
    elif stat.S_IMODE(info.st_mode) != 0o700:
        os.close(descriptor)
        raise UnsafeArtifactRootError("existing managed artifact directory must have mode 0700")
    return descriptor


def _store_chunks(
    directory_descriptor: int, chunks: Iterable[bytes], maximum_size: int
) -> tuple[str, int, str]:
    """Write bounded byte chunks to a private temporary file and return its digest."""
    temporary_name = f".tmp-{secrets.token_hex(16)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    descriptor = os.open(temporary_name, flags, 0o600, dir_fd=directory_descriptor)
    digest = hashlib.sha256()
    size = 0
    try:
        os.fchmod(descriptor, 0o600)
        for chunk in chunks:
            if not isinstance(chunk, bytes):
                raise RawEvidenceError("response chunks must be bytes")
            if len(chunk) > maximum_size - size:
                raise ResponseTooLargeError("response body exceeds the configured byte limit")
            _write_all(descriptor, chunk)
            digest.update(chunk)
            size += len(chunk)
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        _unlink_if_present(directory_descriptor, temporary_name)
        raise
    else:
        os.close(descriptor)
    return digest.hexdigest(), size, temporary_name


def _write_all(descriptor: int, value: bytes) -> None:
    """Write all bytes, handling short writes from the operating system."""
    view = memoryview(value)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write while storing raw response")
        view = view[written:]


def _publish_existing_temp(
    *,
    source_directory_fd: int,
    destination_fd: int,
    temporary_name: str,
    final_name: str,
    expected_hash: str,
    expected_size: int,
    maximum_size: int,
) -> None:
    """Publish a temp file without clobbering and verify a concurrent winner."""
    # Cross-directory hard links remain on the explicitly supplied artifact root's
    # filesystem and give atomic no-clobber publication.
    try:
        os.link(
            temporary_name,
            final_name,
            src_dir_fd=source_directory_fd,
            dst_dir_fd=destination_fd,
            follow_symlinks=False,
        )
        os.fsync(destination_fd)
    except FileExistsError:
        _verify_file(destination_fd, final_name, expected_hash, expected_size, maximum_size)
    finally:
        _unlink_if_present(source_directory_fd, temporary_name)


def _store_bytes_no_clobber(
    directory_descriptor: int, final_name: str, content: bytes, *, expected_hash: str
) -> None:
    """Atomically persist immutable manifest bytes without replacing a winner."""
    temporary_name = f".tmp-{secrets.token_hex(16)}"
    descriptor = os.open(
        temporary_name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory_descriptor,
    )
    try:
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, content)
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        _unlink_if_present(directory_descriptor, temporary_name)
        raise
    else:
        os.close(descriptor)
    try:
        try:
            os.link(
                temporary_name,
                final_name,
                src_dir_fd=directory_descriptor,
                dst_dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            os.fsync(directory_descriptor)
        except FileExistsError:
            _verify_file(
                directory_descriptor,
                final_name,
                expected_hash,
                len(content),
                _MAX_MANIFEST_BYTES,
            )
    finally:
        _unlink_if_present(directory_descriptor, temporary_name)


def _verify_file(
    directory_descriptor: int,
    name: str,
    expected_hash: str,
    expected_size: int,
    maximum_size: int,
) -> None:
    """Verify a pre-existing content-addressed object without following links."""
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory_descriptor,
        )
    except OSError as exc:
        raise ArtifactIntegrityError("existing artifact cannot be opened safely") from exc
    digest = hashlib.sha256()
    size = 0
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise ArtifactIntegrityError("existing artifact is not a private regular file")
        while True:
            chunk = os.read(descriptor, min(64 * 1024, maximum_size - size + 1))
            if not chunk:
                break
            size += len(chunk)
            if size > maximum_size:
                raise ArtifactIntegrityError("existing artifact exceeds its configured size limit")
            digest.update(chunk)
    finally:
        os.close(descriptor)
    if size != expected_size or digest.hexdigest() != expected_hash:
        raise ArtifactIntegrityError(
            "existing content-addressed artifact failed hash or size verification"
        )


def _unlink_if_present(directory_descriptor: int, name: str) -> None:
    """Remove a temporary name while treating prior cleanup as idempotent."""
    try:
        os.unlink(name, dir_fd=directory_descriptor)
    except FileNotFoundError:
        return
    os.fsync(directory_descriptor)
