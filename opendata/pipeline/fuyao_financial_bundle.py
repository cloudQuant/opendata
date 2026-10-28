"""Load a narrowly scoped, externally pinned Fuyao financial configuration.

The caller supplies the deployment-owned path and the expected SHA-256 digest
separately. The digest pins the exact file bytes; it is not a signature, source
rights grant, independent review, or proof that declared units are true. This
module reads no environment variables, credentials, databases, or network
resources, and it never writes or provisions SourceAttemptGate grants.
"""

from __future__ import annotations

import errno
import hashlib
import hmac
import json
import os
import re
import stat
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, TypeGuard

from opendata.data.instrument_reference import (
    InstrumentReferenceRegistry,
    LifecycleStatus,
)
from opendata.data.instrument_reference_snapshot import (
    InstrumentReferenceSnapshotError,
    decode_instrument_reference_snapshot,
)
from opendata.data.providers.ths.endpoints import financial_statement_items
from opendata.data.providers.ths.models.financial_observation_fetcher import (
    ThsFinancialNativeUnitProfile,
    canonical_native_unit_mapping_sha256,
)

if TYPE_CHECKING:
    from opendata.data.financial_observation import StatementType

MAX_FUYAO_FINANCIAL_BUNDLE_BYTES = 1024 * 1024
_BUNDLE_SCHEMA_VERSION = 1
_MAX_JSON_DEPTH = 12
_MAX_JSON_NODES = 20_000
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_INSTRUMENT_PATTERN = re.compile(r"^[0-9]{6}\.(?:SH|SZ|BJ)$")
_STATEMENTS = frozenset({"income", "balance", "cashflow"})
_DATASET_ID = "financial_statement"
_SOURCE_ID = "ths"
_ROOT_FIELDS = frozenset(
    {"schema_version", "scope", "instrument_reference_snapshot", "unit_profiles"}
)
_SCOPE_FIELDS = frozenset({"instrument_id"})
_UNIT_PROFILE_FIELDS = frozenset(
    {"statement_type", "version", "native_units", "mapping_sha256_declared", "evidence_ref"}
)
_UNSAFE_DIRECTORY_WRITE_BITS = stat.S_IWGRP | stat.S_IWOTH
_UNSAFE_FILE_MODE_BITS = stat.S_IWGRP | stat.S_IWOTH | stat.S_ISUID | stat.S_ISGID


class FuyaoFinancialBundleError(ValueError):
    """Stable, content-free error from loading a pinned Fuyao bundle."""

    def __init__(self, code: str) -> None:
        """Create a safe error containing only a fixed machine-readable code."""
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class FuyaoFinancialBundle:
    """One canonical instrument with validated registry and operator unit declarations.

    ``bundle_sha256`` identifies the exact bytes accepted by the loader only
    when the object came from :func:`load_fuyao_financial_bundle`. Direct
    construction accepts a caller-supplied digest declaration; the object
    cannot establish that its bytes were pinned or independently reviewed.
    Even a loader-accepted pin and the contained evidence references and units
    do not prove legal authorization or semantic truth.
    """

    instrument_id: str
    instrument_registry: InstrumentReferenceRegistry = field(repr=False)
    unit_profiles: Mapping[str, ThsFinancialNativeUnitProfile] = field(repr=False)
    bundle_sha256: str

    def __post_init__(self) -> None:
        """Freeze the profile mapping and reject malformed direct construction."""
        if (
            type(self.instrument_id) is not str
            or _INSTRUMENT_PATTERN.fullmatch(self.instrument_id) is None
        ):
            raise FuyaoFinancialBundleError("bundle_instrument_scope_invalid")
        if type(self.instrument_registry) is not InstrumentReferenceRegistry:
            raise FuyaoFinancialBundleError("bundle_registry_invalid")
        _validate_single_instrument_registry(self.instrument_registry, self.instrument_id)
        if (
            type(self.bundle_sha256) is not str
            or _SHA256_PATTERN.fullmatch(self.bundle_sha256) is None
        ):
            raise FuyaoFinancialBundleError("bundle_pin_invalid")
        if not isinstance(self.unit_profiles, Mapping):
            raise FuyaoFinancialBundleError("bundle_unit_profiles_invalid")
        try:
            profiles = dict(self.unit_profiles.items())
        except Exception:
            raise FuyaoFinancialBundleError("bundle_unit_profiles_invalid") from None
        if set(profiles) != _STATEMENTS:
            raise FuyaoFinancialBundleError("bundle_unit_profiles_invalid")
        for statement, profile in profiles.items():
            _validate_unit_profile(statement, profile)
        object.__setattr__(self, "unit_profiles", MappingProxyType(profiles))


def load_fuyao_financial_bundle(*, path: str | Path, expected_sha256: str) -> FuyaoFinancialBundle:
    """Read and validate one exact-pinned, single-instrument configuration.

    The path and digest must come from deployment-controlled configuration,
    never from a run request or the bundle itself. A job must still compare its
    requested instrument with ``bundle.instrument_id`` and validate all other
    business inputs before creating a fetch request.

    Args:
        path: Absolute deployment-owned bundle path. Every path component is
            opened without following symlinks.
        expected_sha256: Independently injected lowercase SHA-256 pin for the
            complete file bytes.

    Returns:
        A frozen bundle containing exactly one canonical instrument, its
        validated lifecycle registry, and all three operator-declared unit
        profiles.

    Raises:
        FuyaoFinancialBundleError: If the path, pin, bytes, schema, identity,
            registry, or unit declarations fail closed validation.
    """
    if type(expected_sha256) is not str or _SHA256_PATTERN.fullmatch(expected_sha256) is None:
        raise FuyaoFinancialBundleError("bundle_pin_invalid")
    raw = _read_atomic_regular_file(path)
    actual_sha256 = hashlib.sha256(raw).hexdigest()
    if not hmac.compare_digest(actual_sha256, expected_sha256):
        raise FuyaoFinancialBundleError("bundle_pin_mismatch")
    document = _decode_json_document(raw)
    root = _object(document, _ROOT_FIELDS, "bundle")
    if type(root["schema_version"]) is not int or root["schema_version"] != _BUNDLE_SCHEMA_VERSION:
        raise FuyaoFinancialBundleError("bundle_schema_invalid")

    scope = _object(root["scope"], _SCOPE_FIELDS, "scope")
    instrument_id = _instrument_id(scope["instrument_id"])
    registry = _decode_registry(root["instrument_reference_snapshot"])
    _validate_single_instrument_registry(registry, instrument_id)
    profiles = _decode_unit_profiles(root["unit_profiles"])
    return FuyaoFinancialBundle(
        instrument_id=instrument_id,
        instrument_registry=registry,
        unit_profiles=profiles,
        bundle_sha256=actual_sha256,
    )


def _read_atomic_regular_file(path: str | Path) -> bytes:
    """Read one bounded regular file through pinned directory descriptors."""
    if not isinstance(path, (str, Path)) or isinstance(path, bool):
        raise FuyaoFinancialBundleError("bundle_path_invalid")
    try:
        config_path = Path(path)
    except (TypeError, ValueError, OSError):
        raise FuyaoFinancialBundleError("bundle_path_invalid") from None
    if not config_path.is_absolute():
        raise FuyaoFinancialBundleError("bundle_path_invalid")
    parts = config_path.parts
    components = parts[1:]
    if not components or any(part in {"", ".", ".."} or "\x00" in part for part in components):
        raise FuyaoFinancialBundleError("bundle_path_invalid")

    required_flags = ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC", "O_NONBLOCK")
    if any(not hasattr(os, name) for name in required_flags):
        raise FuyaoFinancialBundleError("bundle_platform_unsupported")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    directory_fds: list[int] = []
    directory_snapshots: list[os.stat_result] = []
    file_fd: int | None = None
    try:
        root_fd = os.open(os.sep, directory_flags)
        directory_fds.append(root_fd)
        root_stat = os.fstat(root_fd)
        directory_snapshots.append(root_stat)
        _check_directory_permissions(root_stat, is_parent=False)

        directory_names = components[:-1]
        for index, name in enumerate(directory_names):
            child_fd = os.open(name, directory_flags, dir_fd=directory_fds[-1])
            directory_fds.append(child_fd)
            child_stat = os.fstat(child_fd)
            directory_snapshots.append(child_stat)
            _check_directory_permissions(child_stat, is_parent=index == len(directory_names) - 1)

        parent_fd = directory_fds[-1]
        if not directory_names:
            _check_directory_permissions(os.fstat(parent_fd), is_parent=True)
        filename = components[-1]
        named_before = os.stat(filename, dir_fd=parent_fd, follow_symlinks=False)
        _check_file_permissions(named_before)
        file_fd = os.open(filename, file_flags, dir_fd=parent_fd)
        before = os.fstat(file_fd)
        _check_file_permissions(before)
        if not _same_file_snapshot(named_before, before):
            raise FuyaoFinancialBundleError("bundle_path_changed_during_read")
        if before.st_size <= 0 or before.st_size > MAX_FUYAO_FINANCIAL_BUNDLE_BYTES:
            raise FuyaoFinancialBundleError("bundle_file_size_invalid")

        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(
                file_fd,
                min(65_536, MAX_FUYAO_FINANCIAL_BUNDLE_BYTES + 1 - total),
            )
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_FUYAO_FINANCIAL_BUNDLE_BYTES:
                raise FuyaoFinancialBundleError("bundle_file_size_invalid")
        raw = b"".join(chunks)
        after = os.fstat(file_fd)
        _verify_open_path_binding(
            directory_fds,
            directory_snapshots,
            directory_names,
            parent_fd,
            filename,
            file_fd,
        )
        if not _same_file_snapshot(before, after) or len(raw) != after.st_size:
            raise FuyaoFinancialBundleError("bundle_file_changed_during_read")
        return raw
    except FuyaoFinancialBundleError:
        raise
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise FuyaoFinancialBundleError("bundle_symlink_rejected") from None
        raise FuyaoFinancialBundleError("bundle_file_unavailable") from None
    finally:
        if file_fd is not None:
            with suppress(OSError):
                os.close(file_fd)
        for directory_fd in reversed(directory_fds):
            with suppress(OSError):
                os.close(directory_fd)


def _check_directory_permissions(info: os.stat_result, *, is_parent: bool) -> None:
    """Require owned, non-writable directories except root-owned sticky ancestors."""
    if not stat.S_ISDIR(info.st_mode):
        raise FuyaoFinancialBundleError("bundle_path_invalid")
    effective_uid = os.geteuid()
    if info.st_uid not in {0, effective_uid}:
        raise FuyaoFinancialBundleError("bundle_permissions_invalid")
    mode = stat.S_IMODE(info.st_mode)
    writable_by_others = bool(mode & _UNSAFE_DIRECTORY_WRITE_BITS)
    root_sticky_directory = info.st_uid == 0 and bool(mode & stat.S_ISVTX)
    if writable_by_others and not root_sticky_directory:
        raise FuyaoFinancialBundleError("bundle_permissions_invalid")
    if is_parent and (info.st_uid != effective_uid or writable_by_others):
        raise FuyaoFinancialBundleError("bundle_permissions_invalid")


def _check_file_permissions(info: os.stat_result) -> None:
    """Reject links, special files, foreign ownership, and unsafe write modes."""
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise FuyaoFinancialBundleError("bundle_file_type_invalid")
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & _UNSAFE_FILE_MODE_BITS:
        raise FuyaoFinancialBundleError("bundle_permissions_invalid")


def _same_file_snapshot(before: os.stat_result, after: os.stat_result) -> bool:
    """Compare identity, permissions, and mutation-sensitive metadata."""
    fields = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_uid",
        "st_gid",
        "st_nlink",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    )
    return all(getattr(before, field_name) == getattr(after, field_name) for field_name in fields)


def _verify_open_path_binding(
    directory_fds: list[int],
    directory_snapshots: list[os.stat_result],
    directory_names: tuple[str, ...],
    parent_fd: int,
    filename: str,
    file_fd: int,
) -> None:
    """Ensure the original names still identify the exact descriptors read."""
    root_before = directory_snapshots[0]
    root_current = os.fstat(directory_fds[0])
    _check_directory_permissions(root_current, is_parent=not directory_names)
    if not _same_directory_snapshot(
        root_before, root_current, include_change_time=not directory_names
    ):
        raise FuyaoFinancialBundleError("bundle_path_changed_during_read")
    for index, name in enumerate(directory_names):
        child_fd = directory_fds[index + 1]
        expected = directory_snapshots[index + 1]
        current = os.fstat(child_fd)
        is_parent = index + 1 == len(directory_fds) - 1
        _check_directory_permissions(current, is_parent=is_parent)
        actual = os.stat(name, dir_fd=directory_fds[index], follow_symlinks=False)
        if (
            not _same_directory_snapshot(expected, current, include_change_time=is_parent)
            or not _same_directory_snapshot(current, actual, include_change_time=is_parent)
            or not stat.S_ISDIR(actual.st_mode)
        ):
            raise FuyaoFinancialBundleError("bundle_path_changed_during_read")
    expected_file = os.fstat(file_fd)
    actual_file = os.stat(filename, dir_fd=parent_fd, follow_symlinks=False)
    if not _same_file_snapshot(expected_file, actual_file):
        raise FuyaoFinancialBundleError("bundle_path_changed_during_read")
    _check_file_permissions(expected_file)


def _same_directory_snapshot(
    before: os.stat_result,
    after: os.stat_result,
    *,
    include_change_time: bool,
) -> bool:
    """Detect directory ownership, permissions, or path-entry changes during read."""
    fields = ("st_dev", "st_ino", "st_mode", "st_uid", "st_gid")
    stable = all(getattr(before, field_name) == getattr(after, field_name) for field_name in fields)
    return stable and (not include_change_time or before.st_ctime_ns == after.st_ctime_ns)


def _decode_json_document(raw: bytes) -> object:
    """Decode bounded strict JSON without exposing parser details to callers."""
    try:
        text = raw.decode("utf-8", errors="strict")
        document = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
        _check_json_budget(document)
        _check_json_unicode_scalars(document)
        return document
    except FuyaoFinancialBundleError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, TypeError, ValueError):
        raise FuyaoFinancialBundleError("bundle_json_invalid") from None


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate JSON keys at every object depth."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise FuyaoFinancialBundleError("bundle_json_duplicate_key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    """Reject non-standard JSON NaN and Infinity tokens."""
    raise FuyaoFinancialBundleError("bundle_json_invalid")


def _check_json_budget(document: object) -> None:
    """Bound parsed node count and nesting before any schema-specific work."""
    stack: list[tuple[object, int]] = [(document, 1)]
    nodes = 0
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if depth > _MAX_JSON_DEPTH or nodes > _MAX_JSON_NODES:
            raise FuyaoFinancialBundleError("bundle_json_bounds_invalid")
        if type(value) is dict:
            stack.extend((child, depth + 1) for child in value.values())
        elif type(value) is list:
            stack.extend((child, depth + 1) for child in value)


def _check_json_unicode_scalars(document: object) -> None:
    """Reject escaped lone surrogates before text reaches domain validators."""
    stack: list[object] = [document]
    while stack:
        value = stack.pop()
        if type(value) is str:
            try:
                value.encode("utf-8", errors="strict")
            except UnicodeEncodeError:
                raise FuyaoFinancialBundleError("bundle_json_unicode_invalid") from None
        elif type(value) is dict:
            for key, child in value.items():
                stack.append(key)
                stack.append(child)
        elif type(value) is list:
            stack.extend(value)


def _object(value: object, fields: frozenset[str], name: str) -> dict[str, object]:
    """Require an exact object shape without echoing keys or values."""
    if type(value) is not dict or set(value) != fields:
        code_by_name = {
            "bundle": "bundle_schema_invalid",
            "scope": "bundle_scope_schema_invalid",
            "unit_profile": "bundle_unit_profile_schema_invalid",
        }
        raise FuyaoFinancialBundleError(code_by_name[name])
    return value


def _instrument_id(value: object) -> str:
    """Require one exact exchange-qualified six-digit instrument token."""
    if type(value) is not str or _INSTRUMENT_PATTERN.fullmatch(value) is None:
        raise FuyaoFinancialBundleError("bundle_instrument_scope_invalid")
    return value


def _decode_registry(value: object) -> InstrumentReferenceRegistry:
    """Validate the embedded registry through the existing strict snapshot codec."""
    if type(value) is not dict:
        raise FuyaoFinancialBundleError("bundle_registry_invalid")
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", errors="strict")
        registry = decode_instrument_reference_snapshot(encoded)
    except (
        InstrumentReferenceSnapshotError,
        UnicodeEncodeError,
        TypeError,
        ValueError,
        RecursionError,
    ):
        raise FuyaoFinancialBundleError("bundle_registry_invalid") from None
    return registry


def _validate_single_instrument_registry(
    registry: InstrumentReferenceRegistry, instrument_id: str
) -> None:
    """Bind exactly one non-unknown THS financial identity to the bundle scope."""
    if len(registry.lifecycles) != 1 or registry.aliases:
        raise FuyaoFinancialBundleError("bundle_registry_scope_invalid")
    lifecycle = registry.lifecycles[0]
    if (
        lifecycle.dataset_id != _DATASET_ID
        or lifecycle.source_id != _SOURCE_ID
        or lifecycle.instrument_id != instrument_id
        or lifecycle.status is LifecycleStatus.UNKNOWN
    ):
        raise FuyaoFinancialBundleError("bundle_registry_scope_invalid")


def _decode_unit_profiles(value: object) -> Mapping[str, ThsFinancialNativeUnitProfile]:
    """Build all statement profiles and compare item coverage with the provider map."""
    if type(value) is not dict or set(value) != _STATEMENTS:
        raise FuyaoFinancialBundleError("bundle_unit_profiles_invalid")
    profiles: dict[str, ThsFinancialNativeUnitProfile] = {}
    for statement in sorted(_STATEMENTS):
        if not _is_statement_type(statement):
            raise FuyaoFinancialBundleError("bundle_unit_profiles_invalid")
        raw_profile = _object(value[statement], _UNIT_PROFILE_FIELDS, "unit_profile")
        if raw_profile["statement_type"] != statement:
            raise FuyaoFinancialBundleError("bundle_unit_profile_invalid")
        native_units = raw_profile["native_units"]
        if type(native_units) is not dict:
            raise FuyaoFinancialBundleError("bundle_unit_profile_invalid")
        version = raw_profile["version"]
        declared_digest = raw_profile["mapping_sha256_declared"]
        evidence_ref = raw_profile["evidence_ref"]
        try:
            if (
                type(version) is not str
                or type(declared_digest) is not str
                or type(evidence_ref) is not str
            ):
                raise ValueError("unit profile field types are invalid")
            profiles[statement] = ThsFinancialNativeUnitProfile(
                statement_type=statement,
                version=version,
                native_units=native_units,
                mapping_sha256_declared=declared_digest,
                evidence_ref=evidence_ref,
            )
            _validate_unit_profile(statement, profiles[statement])
        except (KeyError, RecursionError, RuntimeError, TypeError, ValueError):
            raise FuyaoFinancialBundleError("bundle_unit_profile_invalid") from None
    return MappingProxyType(profiles)


def _validate_unit_profile(statement: str, profile: object) -> ThsFinancialNativeUnitProfile:
    """Recheck one profile's statement, digest, and complete provider item set."""
    if (
        not _is_statement_type(statement)
        or type(profile) is not ThsFinancialNativeUnitProfile
        or profile.statement_type != statement
    ):
        raise FuyaoFinancialBundleError("bundle_unit_profile_invalid")
    try:
        calculated_digest = canonical_native_unit_mapping_sha256(
            statement_type=statement,
            version=profile.version,
            native_units=profile.native_units,
        )
        expected_items = set(financial_statement_items(statement))
        if (
            not hmac.compare_digest(calculated_digest, profile.mapping_sha256_declared)
            or set(profile.native_units) != expected_items
        ):
            raise FuyaoFinancialBundleError("bundle_unit_profile_invalid")
    except FuyaoFinancialBundleError:
        raise
    except (KeyError, RecursionError, RuntimeError, TypeError, ValueError):
        raise FuyaoFinancialBundleError("bundle_unit_profile_invalid") from None
    return profile


def _is_statement_type(value: object) -> TypeGuard[StatementType]:
    """Return whether the supplied string is one of the exact statement tokens."""
    return type(value) is str and value in _STATEMENTS


__all__ = [
    "MAX_FUYAO_FINANCIAL_BUNDLE_BYTES",
    "FuyaoFinancialBundle",
    "FuyaoFinancialBundleError",
    "load_fuyao_financial_bundle",
]
