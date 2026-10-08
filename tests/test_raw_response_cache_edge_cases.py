"""Regression edges for raw response cache identity, decoding, and failures."""

from __future__ import annotations

import json
import logging
import struct
from typing import TYPE_CHECKING

import pytest

from opendata.data.raw_response_cache import RawResponseCache

if TYPE_CHECKING:
    from pathlib import Path


def _put(
    cache: RawResponseCache,
    *,
    params: dict[str, object] | None = None,
    content: bytes = b"provider-body",
    status_code: int = 200,
    encoding: str | None = "utf-8",
) -> bool:
    return cache.put(
        method="GET",
        source="fred",
        endpoint="https://cache.example/series",
        params=params,
        status_code=status_code,
        content=content,
        encoding=encoding,
    )


def _get(cache: RawResponseCache, *, params: dict[str, object] | None = None):
    return cache.get(
        source="fred",
        endpoint="https://cache.example/series",
        params=params,
    )


@pytest.mark.parametrize(
    ("stored_value", "different_value"),
    [(True, 1), (1, "1")],
    ids=["bool-vs-int", "int-vs-string"],
)
def test_typed_request_values_keep_separate_cache_identities(
    tmp_path: Path, stored_value: object, different_value: object
) -> None:
    cache = RawResponseCache(tmp_path, ttl_seconds=60)

    assert _put(cache, params={"limit": stored_value}, content=b"typed-response")

    assert _get(cache, params={"limit": stored_value}).content == b"typed-response"
    assert _get(cache, params={"limit": different_value}) is None


def test_invalidate_removes_only_the_matching_request_identity(tmp_path: Path) -> None:
    cache = RawResponseCache(tmp_path, ttl_seconds=60)
    first_params = {"series_id": "CPI"}
    second_params = {"series_id": "PCE"}

    assert _put(cache, params=first_params, content=b"cpi-response")
    assert _put(cache, params=second_params, content=b"pce-response")
    cache.invalidate(source="fred", endpoint="https://cache.example/series", params=first_params)

    assert _get(cache, params=first_params) is None
    assert _get(cache, params=second_params).content == b"pce-response"
    assert len(list(cache.raw_dir.glob("*.cache"))) == 1


@pytest.mark.parametrize(
    "ttl_seconds",
    [0, -1, True, 1.5],
    ids=["zero", "negative", "bool-is-not-int", "non-integer"],
)
def test_invalid_ttl_is_rejected_at_construction(tmp_path: Path, ttl_seconds: object) -> None:
    with pytest.raises(ValueError, match="ttl_seconds must be positive"):
        RawResponseCache(tmp_path, ttl_seconds=ttl_seconds)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "schema_version",
    [None, True, "", "   ", 0, -1, 1.5],
    ids=["none", "bool", "empty", "whitespace", "zero", "negative", "non-integer"],
)
def test_invalid_schema_version_is_rejected_at_construction(
    tmp_path: Path, schema_version: object
) -> None:
    with pytest.raises(ValueError, match="schema_version must be a non-empty"):
        RawResponseCache(tmp_path, ttl_seconds=60, schema_version=schema_version)  # type: ignore[arg-type]


def test_non_finite_clock_is_a_safe_write_failure_and_read_miss(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    now = [float("nan")]
    cache = RawResponseCache(tmp_path, ttl_seconds=60, clock=lambda: now[0])

    with caplog.at_level(logging.WARNING, logger="opendata.data.raw_response_cache"):
        assert not _put(cache)
        assert not cache.raw_dir.exists()

        now[0] = 100.0
        assert _put(cache)
        now[0] = float("inf")
        assert _get(cache) is None

    warnings = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "raw_response_cache_warning"
    ]
    assert [(record.reason, record.error_type) for record in warnings] == [
        ("write_failed", "ValueError"),
        ("entry_invalid", "ValueError"),
    ]


@pytest.mark.parametrize("tamper", ["truncated_body", "forged_key"], ids=["truncated", "wrong-key"])
def test_incomplete_or_forged_entry_is_a_safe_miss(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, tamper: str
) -> None:
    cache = RawResponseCache(tmp_path, ttl_seconds=60)
    assert _put(cache)
    entry = next(cache.raw_dir.glob("*.cache"))
    record = entry.read_bytes()

    if tamper == "truncated_body":
        entry.write_bytes(record[:-2])
    else:
        magic = b"ODRAW1\n"
        length_offset = len(magic)
        header_length = struct.unpack_from(">I", record, length_offset)[0]
        body_start = length_offset + struct.calcsize(">I") + header_length
        metadata = json.loads(record[length_offset + struct.calcsize(">I") : body_start])
        metadata["key"] = "0" * 64
        encoded = json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode()
        entry.write_bytes(magic + struct.pack(">I", len(encoded)) + encoded + record[body_start:])

    with caplog.at_level(logging.WARNING, logger="opendata.data.raw_response_cache"):
        assert _get(cache) is None

    assert any(
        getattr(record, "event", None) == "raw_response_cache_warning"
        and getattr(record, "reason", None) == "entry_invalid"
        for record in caplog.records
    )


def test_cached_response_rebuilds_safe_metadata_and_discards_unsafe_encoding(
    tmp_path: Path,
) -> None:
    now = [1_800_000_000.0]
    cache = RawResponseCache(tmp_path, ttl_seconds=30, clock=lambda: now[0])

    assert _put(cache, content=b"plain-response", status_code=206, encoding="utf-8")
    response = _get(cache)
    assert response is not None
    assert (response.status_code, response.content, response.encoding) == (
        206,
        b"plain-response",
        "utf-8",
    )
    assert (response.created_at, response.expires_at) == (1_800_000_000.0, 1_800_000_030.0)

    assert _put(cache, params={"variant": "unsafe-encoding"}, encoding="utf-8\nInjected: yes")
    unsafe_response = _get(cache, params={"variant": "unsafe-encoding"})
    assert unsafe_response is not None
    assert unsafe_response.content == b"provider-body"
    assert unsafe_response.encoding is None
    serialized = b"".join(entry.read_bytes() for entry in cache.raw_dir.glob("*.cache"))
    assert b"Injected: yes" not in serialized


@pytest.mark.parametrize("cleanup_fails", [False, True], ids=["cleanup-ok", "cleanup-fails"])
def test_failed_atomic_replace_is_nonfatal_and_reports_or_cleans_temp_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    cleanup_fails: bool,
) -> None:
    import opendata.data.raw_response_cache as cache_module

    cache = RawResponseCache(tmp_path, ttl_seconds=60)
    original_unlink = cache_module.os.unlink

    def fail_replace(*args, **kwargs) -> None:
        raise OSError("injected replace failure")

    def optionally_fail_temp_unlink(path, *args, **kwargs) -> None:
        if cleanup_fails and isinstance(path, str) and path.endswith(".tmp"):
            raise PermissionError("injected cleanup failure")
        original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(cache_module.os, "replace", fail_replace)
        patch.setattr(cache_module.os, "unlink", optionally_fail_temp_unlink)
        with caplog.at_level(logging.WARNING, logger="opendata.data.raw_response_cache"):
            assert not _put(cache)
            assert _get(cache) is None

    assert not list(cache.raw_dir.glob("*.cache"))
    temporary_entries = list(cache.raw_dir.glob("*.tmp"))
    assert bool(temporary_entries) is cleanup_fails
    assert any(
        getattr(record, "event", None) == "raw_response_cache_warning"
        and getattr(record, "reason", None) == "write_failed"
        and getattr(record, "error_type", None) == "OSError"
        for record in caplog.records
    )
    for temporary_entry in temporary_entries:
        temporary_entry.unlink()
