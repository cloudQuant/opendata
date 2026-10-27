"""Read and verify one locally managed raw response addressed by its manifest digest.

The manifest digest is an integrity address, not a signature or proof of source
authenticity or clock accuracy. `captured_at_not_after` is a trusted caller's UTC
bound; the digest does not prove that the source clock or collector clock was true.
Directory descriptors and private modes protect against other local users, but a
process with the same UID can still replace or alter evidence. Hard links are
allowed because the writer uses them briefly for atomic publication; a same-UID
process can modify their shared inode, which the body digest detects when read.
This reader additionally requires the artifact root to belong to the current
effective UID, which is stricter than the writer's mode-only check for an existing
root. It grants no API access, does not make a backup, and does not establish rights
to collect, retain, or redistribute source data. It performs no environment,
network, or database access. The response limit caps body bytes; assembly and
conversion to immutable `bytes` may transiently require roughly twice that limit,
so this reader caps configured bodies at 64 MiB.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from opendata.pipeline.raw_evidence import RawResponseManifest, RawResponseSource

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MAX_MANIFEST_BYTES = 16 * 1024
_DEFAULT_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_MAX_CONFIGURED_RESPONSE_BYTES = 64 * 1024 * 1024


class RawEvidenceReadError(ValueError):
    """Raised when a raw evidence reference cannot be safely resolved and verified."""


class RawEvidenceReadInputError(RawEvidenceReadError):
    """Raised when caller-supplied evidence identity or limits are invalid."""


class UnsafeRawEvidenceError(RawEvidenceReadError):
    """Raised when a managed path or file violates private-storage requirements."""


class RawEvidenceIntegrityError(RawEvidenceReadError):
    """Raised when addressed manifest or response bytes fail integrity checks."""


class RawEvidenceContextMismatchError(RawEvidenceReadError):
    """Raised when an immutable manifest does not match the expected request context."""


@dataclass(frozen=True, slots=True)
class RawEvidenceReadResult:
    """Verified original response bytes and the exact validated manifest."""

    body: bytes = field(repr=False)
    manifest: RawResponseManifest
    manifest_sha256: str


class RawResponseArtifactReader:
    """Read one manifest and body below an explicit, private artifact root."""

    def __init__(
        self,
        artifact_root: Path | str,
        *,
        max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        """Bind a reader to one absolute artifact root and finite response limit."""
        if not isinstance(artifact_root, (str, Path)):
            raise RawEvidenceReadInputError("artifact_root must be an explicit absolute path")
        root = Path(artifact_root)
        if not root.is_absolute() or len(root.parts) < 2 or ".." in root.parts:
            raise RawEvidenceReadInputError("artifact_root must be a non-root absolute path")
        if isinstance(max_response_bytes, bool) or not isinstance(max_response_bytes, int):
            raise RawEvidenceReadInputError("max_response_bytes must be an integer")
        if not 1 <= max_response_bytes <= _MAX_CONFIGURED_RESPONSE_BYTES:
            raise RawEvidenceReadInputError("max_response_bytes must use a finite supported limit")
        self._artifact_root = root
        self._max_response_bytes = max_response_bytes

    def read(
        self,
        manifest_sha256: str,
        *,
        expected_source: RawResponseSource,
        expected_request_summary_sha256: str,
        captured_at_not_after: datetime,
        expected_http_status: int = 200,
    ) -> RawEvidenceReadResult:
        """Resolve one digest beneath the bound root and verify its full request context.

        Empty response bodies are valid evidence when the manifest records the
        SHA-256 and size of the empty byte string. Callers remain responsible for
        deciding whether an empty body is meaningful for their dataset.
        """
        _validate_sha256(manifest_sha256, "manifest_sha256")
        source = _validated_source(expected_source)
        _validate_sha256(expected_request_summary_sha256, "expected_request_summary_sha256")
        capture_time_bound = _validated_utc_bound(captured_at_not_after)
        if (
            isinstance(expected_http_status, bool)
            or not isinstance(expected_http_status, int)
            or not 100 <= expected_http_status <= 599
        ):
            raise RawEvidenceReadInputError("expected_http_status must be an HTTP status code")

        root_fd = _open_private_root(self._artifact_root)
        try:
            manifests_fd = _open_private_directory(root_fd, "manifests")
            try:
                manifest_prefix_fd = _open_private_directory(manifests_fd, manifest_sha256[:2])
                try:
                    manifest_name = f"{manifest_sha256}.json"
                    manifest_bytes = _read_private_file(
                        manifest_prefix_fd,
                        manifest_name,
                        maximum=_MAX_MANIFEST_BYTES,
                    )
                finally:
                    os.close(manifest_prefix_fd)
            finally:
                os.close(manifests_fd)

            if hashlib.sha256(manifest_bytes).hexdigest() != manifest_sha256:
                raise RawEvidenceIntegrityError("manifest bytes do not match the requested digest")
            manifest = _parse_canonical_manifest(manifest_bytes)
            if manifest.source != source:
                raise RawEvidenceContextMismatchError("manifest source does not match the request")
            if manifest.request_summary_sha256 != expected_request_summary_sha256:
                raise RawEvidenceContextMismatchError("manifest request digest does not match")
            if manifest.http_status != expected_http_status:
                raise RawEvidenceContextMismatchError("manifest HTTP status does not match")
            if manifest.captured_at_utc > capture_time_bound:
                raise RawEvidenceContextMismatchError(
                    "manifest capture time is after the trusted UTC bound"
                )
            if manifest.response_size_bytes > self._max_response_bytes:
                raise UnsafeRawEvidenceError("manifest response size exceeds the reader limit")

            objects_fd = _open_private_directory(root_fd, "objects")
            try:
                object_prefix_fd = _open_private_directory(objects_fd, manifest.response_sha256[:2])
                try:
                    body = _read_private_file(
                        object_prefix_fd,
                        f"{manifest.response_sha256}.blob",
                        maximum=self._max_response_bytes,
                    )
                finally:
                    os.close(object_prefix_fd)
            finally:
                os.close(objects_fd)
        finally:
            os.close(root_fd)

        if len(body) != manifest.response_size_bytes:
            raise RawEvidenceIntegrityError("response bytes do not match the manifest size")
        if hashlib.sha256(body).hexdigest() != manifest.response_sha256:
            raise RawEvidenceIntegrityError("response bytes do not match the manifest digest")
        return RawEvidenceReadResult(
            body=body,
            manifest=manifest,
            manifest_sha256=manifest_sha256,
        )


def _validate_sha256(value: str, name: str) -> None:
    """Require one lowercase SHA-256 hex address."""
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise RawEvidenceReadInputError(f"{name} must be lowercase SHA-256 hex")


def _validated_source(source: RawResponseSource) -> RawResponseSource:
    """Deeply revalidate the caller's source identity at the reader boundary."""
    if type(source) is not RawResponseSource:
        raise RawEvidenceReadInputError("expected_source must be a validated RawResponseSource")
    try:
        return RawResponseSource.model_validate(source.model_dump(mode="python"), strict=True)
    except (AttributeError, TypeError, ValidationError) as exc:
        raise RawEvidenceReadInputError("expected_source failed validation") from exc


def _validated_utc_bound(value: datetime) -> datetime:
    """Require one explicit aware UTC bound supplied by the trusted caller."""
    if type(value) is not datetime or value.tzinfo is None:
        raise RawEvidenceReadInputError("captured_at_not_after must be an aware UTC datetime")
    try:
        offset = value.utcoffset()
    except (OverflowError, ValueError) as exc:
        raise RawEvidenceReadInputError(
            "captured_at_not_after must be an aware UTC datetime"
        ) from exc
    if offset is None or offset.total_seconds() != 0:
        raise RawEvidenceReadInputError("captured_at_not_after must use UTC offset zero")
    return value


def _parse_canonical_manifest(raw: bytes) -> RawResponseManifest:
    """Reject ambiguous JSON, then validate and require the writer's canonical encoding."""

    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("manifest JSON contains a duplicate key")
            result[key] = value
        return result

    try:
        parsed = json.loads(raw.decode("utf-8"), object_pairs_hook=reject_duplicate_keys)
        if not isinstance(parsed, dict):
            raise ValueError("manifest JSON must be an object")
        manifest = RawResponseManifest.model_validate_json(raw, strict=True)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValidationError) as exc:
        raise RawEvidenceIntegrityError("manifest JSON is invalid") from exc
    except ValueError as exc:
        raise RawEvidenceIntegrityError("manifest JSON is ambiguous or invalid") from exc

    canonical = json.dumps(
        manifest.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if canonical != raw:
        raise RawEvidenceIntegrityError("manifest bytes are not the canonical writer encoding")
    return manifest


def _open_private_root(path: Path) -> int:
    """Open every root component without following links and verify the bound root."""
    _require_safe_platform()
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        components = path.parts[1:]
        for index, component in enumerate(components):
            if not component or component in {".", ".."} or "/" in component or "\\" in component:
                raise UnsafeRawEvidenceError("artifact root contains an unsafe component")
            next_descriptor = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            try:
                info = os.fstat(next_descriptor)
                if not stat.S_ISDIR(info.st_mode):
                    raise UnsafeRawEvidenceError("artifact root component is not a directory")
                if index == len(components) - 1:
                    _require_private_owner(info, 0o700, "artifact root")
            except BaseException:
                os.close(next_descriptor)
                raise
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except OSError as exc:
        os.close(descriptor)
        raise UnsafeRawEvidenceError("artifact root is unavailable or unsafe") from exc
    except BaseException:
        os.close(descriptor)
        raise


def _open_private_directory(parent_fd: int, name: str) -> int:
    """Open one fixed writer-managed directory without following symlinks."""
    if not re.fullmatch(r"[a-z0-9]{1,64}", name):
        raise UnsafeRawEvidenceError("managed directory name is invalid")
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
    except OSError as exc:
        raise UnsafeRawEvidenceError("managed evidence directory is unavailable") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            raise UnsafeRawEvidenceError("managed evidence path is not a directory")
        _require_private_owner(info, 0o700, "managed evidence directory")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read_private_file(directory_fd: int, name: str, *, maximum: int) -> bytes:
    """Read one bounded immutable file and detect inode replacement or mutation."""
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise UnsafeRawEvidenceError("managed evidence filename is invalid")
    _require_safe_platform()
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory_fd,
        )
    except OSError as exc:
        raise UnsafeRawEvidenceError("managed evidence file is unavailable or unsafe") from exc
    try:
        before = os.fstat(descriptor)
        _require_private_file(before, maximum)
        content = bytearray()
        while len(content) <= maximum:
            chunk = os.read(descriptor, min(64 * 1024, maximum + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
        after = os.fstat(descriptor)
        _require_private_file(after, maximum)
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            len(content) != before.st_size
            or after.st_size != before.st_size
            or len(content) > maximum
            or _identity(before) != _identity(after)
            or _identity(after) != _identity(current)
        ):
            raise UnsafeRawEvidenceError("managed evidence file changed while being read")
        return bytes(content)
    except OSError as exc:
        raise UnsafeRawEvidenceError("managed evidence file became unavailable") from exc
    finally:
        os.close(descriptor)


def _require_private_file(info: os.stat_result, maximum: int) -> None:
    """Require one bounded, owner-only regular file owned by this effective user."""
    if not stat.S_ISREG(info.st_mode):
        raise UnsafeRawEvidenceError("managed evidence file must be a regular file")
    _require_private_owner(info, 0o600, "managed evidence file")
    if info.st_size < 0 or info.st_size > maximum:
        raise UnsafeRawEvidenceError("managed evidence file exceeds its byte limit")


def _require_private_owner(info: os.stat_result, mode: int, description: str) -> None:
    """Require effective-user ownership and the writer's exact private mode."""
    if info.st_uid != os.geteuid():
        raise UnsafeRawEvidenceError(f"{description} must be owned by the current user")
    if stat.S_IMODE(info.st_mode) != mode:
        raise UnsafeRawEvidenceError(f"{description} must have mode {mode:04o}")


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    """Return inode identity and write metadata used to detect a concurrent replacement."""
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_mode,
    )


def _require_safe_platform() -> None:
    """Reject platforms missing the descriptor flags needed by this reader."""
    required = ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK", "geteuid")
    if any(not hasattr(os, name) for name in required):
        raise UnsafeRawEvidenceError("platform cannot safely verify raw evidence paths")
