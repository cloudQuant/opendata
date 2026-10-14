#!/usr/bin/env python3
"""Measure matched ODS write/query/export workloads on an owned Docker MySQL.

The default invocation is deliberately inert.  A database is created only
with ``--isolated-run``; that mode creates a task-labelled container and volume,
uses a random loopback port and generated password, and removes only resources
whose immutable IDs and ownership labels still match the run manifest.

This is an offline SQLite-free/MySQL-local comparison, not a production
capacity test or a replay of the C65 source facts.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import importlib.util
import inspect as inspect_module
import json
import math
import os
import platform
import re
import resource
import shutil
import socket
import stat
import subprocess  # guarded local command calls only # nosec B404
import sys
import tarfile
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager, suppress
from datetime import date, datetime, timezone
from datetime import time as datetime_time
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator, Sequence
    from types import FrameType
    from typing import NoReturn, Protocol

    from sqlalchemy.engine import URL, Connection, Engine
    from sqlalchemy.engine.reflection import Inspector
    from sqlalchemy.sql.elements import TextClause

    from opendata.api.schemas import APIResponse

    class _EventApi(Protocol):
        def listen(self, target: Engine, identifier: str, fn: Callable[..., object]) -> object: ...

        def remove(self, target: Engine, identifier: str, fn: Callable[..., object]) -> object: ...


class _SourceFileEntry(TypedDict):
    path: str
    size_bytes: int
    mode_octal: str
    sha256: str


class _VerifiedSourceFileEntry(TypedDict):
    path: str
    sha256: str
    size_bytes: int
    archive_mode_octal: str
    staging_mode_octal: str


BASELINE_COMMIT = "85f95a40a57995c9e46c9a3be1528f416b4f0c70"
MYSQL_IMAGE = (
    "docker.io/library/mysql:8.4.11@"
    "sha256:ca3f0494c0f1fc86eb45f5e4786a1bb9f64d2f85b562cc74a9595046f6519a42"
)
MYSQL_IMAGE_SOURCE = "https://hub.docker.com/_/mysql/tags?name=8.4"
MYSQL_IMAGE_PLATFORM = "linux/arm64/v8"
TABLE = "ods_stock_daily_ths"
KEY = ("thscode", "trade_date")
SOURCE_COLUMNS = (
    "thscode",
    "trade_date",
    "date_ms",
    "currency",
    "interval",
    "adjusted",
    "open_price",
    "high_price",
    "low_price",
    "close_price",
    "volume",
    "turnover",
)
BUSINESS_COLUMNS = SOURCE_COLUMNS
ROWS_PER_SYMBOL = 1_000
SYMBOL_COUNT = 100
EXPECTED_ROWS = ROWS_PER_SYMBOL * SYMBOL_COUNT
FIXTURE_SEED = 20261012
PAGE_ROWS = 10_000
QUERY_PAGE_SIZE = 1_000
EXPORT_LIMIT = EXPECTED_ROWS
WARMUPS = 3
MEASURED_SAMPLES = 20
FIXED_FETCHED_AT = datetime(2026, 10, 12, 0, 0, 0)
FIXED_BATCH_ID = "00000000-0000-4000-8000-000000000001"
RUN_LABEL = "org.opendata.c100.performance.run-id"
PURPOSE_LABEL = "org.opendata.c100.performance.purpose"
PROBE_PURPOSE = "iter2-stock-daily-ods"
_SAFE_SCHEMA = re.compile(r"^opendata_c100_[a-f0-9]{12}_(before|after)_(source|target)$")
_PASSWORD_ENV = "C100_PERF_MYSQL_PASSWORD"  # noqa: S105 -- environment variable name only # nosec B105
_SNAPSHOT_EXCLUDED_DIRS = {
    ".cache",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "artifacts",
    "build",
    "cache",
    "caches",
    "dist",
    "logs",
    "output",
    "outputs",
    "runs",
    "temp",
    "tmp",
}
_SNAPSHOT_SECRET_SUFFIXES = {
    ".cer",
    ".crt",
    ".der",
    ".jks",
    ".key",
    ".keystore",
    ".p12",
    ".pfx",
    ".pem",
    ".secret",
    ".secrets",
}
_SNAPSHOT_GENERATED_SUFFIXES = {".pyc", ".pyo"}
_SNAPSHOT_SECRET_NAMES = {
    "credentials.json",
    "id_ed25519",
    "id_rsa",
    "secrets.json",
}


class _DenyAllNetworkGuard:
    """Block socket/DNS dispatch while rendering a pure source DDL artifact."""

    _SOCKET_METHODS = (
        "accept",
        "bind",
        "connect",
        "connect_ex",
        "listen",
        "recv",
        "recv_into",
        "recvfrom",
        "recvfrom_into",
        "send",
        "sendall",
        "sendmsg",
        "sendto",
    )
    _MODULE_METHODS = (
        "create_connection",
        "getaddrinfo",
        "gethostbyaddr",
        "gethostbyname",
        "gethostbyname_ex",
        "getnameinfo",
    )

    def __init__(self) -> None:
        self.blocked_attempts = 0
        self.original_dispatches: dict[str, int] = {}
        self.blocked_reasons: dict[str, int] = {}
        self.attempts_by_phase: dict[str, int] = {}
        self.current_phase = "guard_setup"
        self.blocked_events: list[dict[str, object]] = []
        self._patched: list[tuple[object, str, object]] = []

    def set_phase(self, phase: str) -> None:
        self.current_phase = phase

    def _deny(self, name: str) -> Callable[..., object]:
        def denied(*_args: object, **_kwargs: object) -> NoReturn:
            self.blocked_attempts += 1
            self.blocked_reasons[name] = self.blocked_reasons.get(name, 0) + 1
            self.attempts_by_phase[self.current_phase] = (
                self.attempts_by_phase.get(self.current_phase, 0) + 1
            )
            if len(self.blocked_events) < 10:
                frames: list[dict[str, object]] = []
                frame: FrameType | None = sys._getframe(1)
                while frame is not None and len(frames) < 12:
                    module_name = frame.f_globals.get("__name__", "")
                    frames.append(
                        {
                            "module": str(module_name)[:160],
                            "file": Path(frame.f_code.co_filename).name,
                            "line": frame.f_lineno,
                            "function": frame.f_code.co_name,
                        }
                    )
                    frame = frame.f_back
                frames.reverse()
                self.blocked_events.append(
                    {
                        "surface": name,
                        "phase": self.current_phase,
                        "stack": frames[-8:],
                    }
                )
            raise PermissionError(f"render_ddl_network_denied:{name}")

        return denied

    def install(self) -> None:
        targets: list[tuple[object, str]] = [(socket, name) for name in self._MODULE_METHODS]
        targets.extend((socket.socket, name) for name in self._SOCKET_METHODS)
        try:
            for target, name in targets:
                original = getattr(target, name, None)
                if original is None:
                    continue
                self.original_dispatches[name] = 0
                self._patched.append((target, name, original))
                setattr(target, name, self._deny(name))
        except Exception:
            self.restore()
            raise

    def restore(self) -> None:
        for target, name, original in reversed(self._patched):
            setattr(target, name, original)
        self._patched.clear()

    @staticmethod
    def _is_known_bootstrap_capability_probe(event: object) -> bool:
        if not isinstance(event, dict):
            return False
        stack = event.get("stack")
        return (
            event.get("surface") == "bind"
            and event.get("phase") == "http_guard_bootstrap"
            and isinstance(stack, list)
            and any(
                isinstance(frame, dict)
                and frame.get("module") == "urllib3.util.connection"
                and frame.get("file") == "connection.py"
                and frame.get("function") == "_has_ipv6"
                for frame in stack
            )
        )

    def evidence(self) -> dict[str, object]:
        return {
            "mode": "deny_all",
            "blocked_attempts": self.blocked_attempts,
            "blocked_reasons": dict(sorted(self.blocked_reasons.items())),
            "attempts_by_phase": dict(sorted(self.attempts_by_phase.items())),
            "blocked_events": list(self.blocked_events),
            "original_dispatches": dict(sorted(self.original_dispatches.items())),
        }


def _snapshot_exclusion_reason(relative: Path, *, is_directory: bool = False) -> str | None:
    """Return a non-sensitive exclusion category for generated or secret files."""
    parts = [part.casefold() for part in relative.parts]
    if any(part in _SNAPSHOT_EXCLUDED_DIRS for part in parts):
        return "generated_directory"
    if any(part == ".env" or part.startswith(".env.") for part in parts):
        return "environment_file"
    if any(part in {".aws", ".ssh"} for part in parts):
        return "credential_directory"
    if is_directory:
        return None
    name = relative.name.casefold()
    suffixes = {suffix.casefold() for suffix in relative.suffixes}
    if suffixes.intersection(_SNAPSHOT_GENERATED_SUFFIXES):
        return "generated_file"
    if (
        name in _SNAPSHOT_SECRET_NAMES
        or name.startswith("service-account")
        or suffixes.intersection(_SNAPSHOT_SECRET_SUFFIXES)
        or suffixes.intersection({".db", ".sqlite", ".sqlite3"})
    ):
        return "secret_or_database_file"
    return None


def _source_inventory(root: Path) -> tuple[list[Path], dict[str, int]]:
    """List regular package files and count exclusions without recording their names."""
    if not root.is_dir():
        raise FileNotFoundError(f"source package is missing: {root}")
    included: list[Path] = []
    excluded: dict[str, int] = {}
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        relative_directory = directory_path.relative_to(root)
        kept_directories: list[str] = []
        for dirname in sorted(dirnames):
            child = relative_directory / dirname
            reason = _snapshot_exclusion_reason(child, is_directory=True)
            if (directory_path / dirname).is_symlink():
                reason = "symlink"
            if reason is None:
                kept_directories.append(dirname)
            else:
                excluded[reason] = excluded.get(reason, 0) + 1
        dirnames[:] = kept_directories
        for filename in sorted(filenames):
            path = directory_path / filename
            relative = path.relative_to(root)
            reason = _snapshot_exclusion_reason(relative)
            if path.is_symlink():
                reason = "symlink"
            if reason is not None:
                excluded[reason] = excluded.get(reason, 0) + 1
            elif path.is_file():
                included.append(path)
    included.sort(key=lambda path: path.relative_to(root).as_posix())
    if not included:
        raise ValueError(f"source package is empty after snapshot filtering: {root}")
    return included, excluded


def _source_tree_state(
    root: Path,
) -> tuple[str, dict[str, dict[str, object]], dict[str, int], dict[str, int]]:
    """Capture a consistent included-source digest and path inventory in one pass."""
    files, excluded = _source_inventory(root)
    digest = hashlib.sha256()
    inventory: dict[str, dict[str, object]] = {}
    for path in files:
        relative = path.relative_to(root).as_posix()
        mode = stat.S_IMODE(path.stat().st_mode)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(f"{mode:04o}".encode("ascii"))
        digest.update(b"\0")
        file_digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
                file_digest.update(chunk)
        digest.update(b"\0")
        inventory[relative] = {
            "sha256": file_digest.hexdigest(),
            "size_bytes": path.stat().st_size,
            "mode_octal": f"{mode:04o}",
        }
    cache_counts = _python_cache_counts(root)
    return digest.hexdigest(), inventory, dict(sorted(excluded.items())), cache_counts


def _python_cache_counts(root: Path) -> dict[str, int]:
    """Count generated Python cache entries without including their names in evidence."""
    directory_count = 0
    bytecode_count = 0
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if path.is_dir() and path.name.casefold() == "__pycache__":
            directory_count += 1
        elif (
            path.is_file()
            and path.suffix.casefold() == ".pyc"
            and "__pycache__" in {part.casefold() for part in relative.parts[:-1]}
        ):
            bytecode_count += 1
    return {"directory_count": directory_count, "bytecode_file_count": bytecode_count}


def _inventory_deltas(
    before: dict[str, dict[str, object]], after: dict[str, dict[str, object]]
) -> list[dict[str, object]]:
    deltas: list[dict[str, object]] = []
    for path in sorted(before.keys() | after.keys()):
        before_entry = before.get(path)
        after_entry = after.get(path)
        if before_entry is None:
            deltas.append({"path": path, "kind": "added", "after": after_entry})
        elif after_entry is None:
            deltas.append({"path": path, "kind": "deleted", "before": before_entry})
        elif before_entry != after_entry:
            deltas.append(
                {
                    "path": path,
                    "kind": "changed",
                    "before": before_entry,
                    "after": after_entry,
                }
            )
    return deltas


def _current_worktree_drift_evidence(
    start_inventory: dict[str, dict[str, object]],
    end_inventory: dict[str, dict[str, object]],
    bundle_manifest: dict[str, object],
    start_tree_sha256: str,
    end_tree_sha256: str,
    start_excluded: dict[str, int],
    end_excluded: dict[str, int],
    start_cache_counts: dict[str, int],
    end_cache_counts: dict[str, int],
) -> dict[str, object]:
    """Describe live-tree drift separately from the immutable worker input."""
    bundle_entries = bundle_manifest.get("files")
    bundle_source_tree_sha256 = bundle_manifest.get("source_package_tree_sha256")
    if not isinstance(bundle_entries, list) or not isinstance(bundle_source_tree_sha256, str):
        raise RuntimeError("source bundle manifest is missing its source inventory")
    bundle_inventory: dict[str, dict[str, object]] = {}
    for entry in bundle_entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise RuntimeError("source bundle inventory entry is malformed")
        name = str(entry["path"])
        if not name.startswith("opendata/"):
            continue
        bundle_inventory[name.removeprefix("opendata/")] = {
            "sha256": entry.get("sha256"),
            "size_bytes": entry.get("size_bytes"),
            "mode_octal": entry.get("mode_octal"),
        }
    start_to_end = _inventory_deltas(start_inventory, end_inventory)
    bundle_to_end = _inventory_deltas(bundle_inventory, end_inventory)
    has_drift = bool(
        start_to_end
        or bundle_to_end
        or start_tree_sha256 != end_tree_sha256
        or start_tree_sha256 != bundle_source_tree_sha256
    )
    return {
        "schema_version": 1,
        "comparison_scope": (
            "included source files only; generated cache paths are excluded "
            "from source hashes and path deltas"
        ),
        "start_tree_sha256": start_tree_sha256,
        "captured_bundle_tree_sha256": bundle_source_tree_sha256,
        "end_tree_sha256": end_tree_sha256,
        "start_matches_captured_bundle": start_tree_sha256 == bundle_source_tree_sha256,
        "end_matches_start": start_tree_sha256 == end_tree_sha256,
        "end_matches_captured_bundle": end_tree_sha256 == bundle_source_tree_sha256,
        "has_worktree_drift": has_drift,
        "start_to_end_source_deltas": start_to_end,
        "captured_bundle_to_end_source_deltas": bundle_to_end,
        "excluded_source_inventory_counts": {
            "at_start": start_excluded,
            "at_end": end_excluded,
        },
        "python_cache_counts": {
            "at_start": start_cache_counts,
            "at_end": end_cache_counts,
            "delta": {
                key: end_cache_counts.get(key, 0) - start_cache_counts.get(key, 0)
                for key in sorted(start_cache_counts.keys() | end_cache_counts.keys())
            },
            "note": (
                "Python cache entries are generated artifacts excluded from "
                "source input comparisons."
            ),
        },
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_source_package(source_root: Path, destination_root: Path) -> dict[str, int]:
    included, excluded = _source_inventory(source_root)
    destination_root.mkdir(parents=True, exist_ok=False)
    for source in included:
        relative = source.relative_to(source_root)
        destination = destination_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    return excluded


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        _json_safe(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _require_list(value: object, description: str) -> list[object]:
    if not isinstance(value, list):
        raise RuntimeError(f"receipt field is not a list: {description}")
    return value


def _parse_source_file_entry(value: object) -> _SourceFileEntry:
    if not isinstance(value, dict):
        raise RuntimeError("source bundle manifest contains a non-object file entry")
    path = value.get("path")
    size_bytes = value.get("size_bytes")
    mode_octal = value.get("mode_octal")
    sha256 = value.get("sha256")
    if (
        not isinstance(path, str)
        or isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or size_bytes < 0
        or not isinstance(mode_octal, str)
        or re.fullmatch(r"[0-7]{4}", mode_octal) is None
        or not isinstance(sha256, str)
        or re.fullmatch(r"[a-f0-9]{64}", sha256) is None
    ):
        raise RuntimeError("source bundle manifest file entry has invalid field types")
    return {
        "path": path,
        "size_bytes": size_bytes,
        "mode_octal": mode_octal,
        "sha256": sha256,
    }


def _json_safe(value: object) -> object:
    """Convert common SQL/Python values into stable strict-JSON values."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "as_tuple") and hasattr(value, "quantize"):
        return str(value)
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _p95(values: Sequence[float]) -> float:
    """Return nearest-rank P95, rejecting absent or invalid latency readings."""
    if not values:
        raise ValueError("P95 needs at least one measurement")
    normalized = sorted(float(value) for value in values)
    if any(not math.isfinite(value) or value < 0 for value in normalized):
        raise ValueError("latencies must be finite and non-negative")
    rank = math.ceil(0.95 * len(normalized))
    return normalized[rank - 1]


def _synthetic_dates() -> list[date]:
    """Build 1,000 deterministic weekdays; these are not an exchange calendar."""
    result: list[date] = []
    candidate = date(2026, 10, 12)
    while len(result) < ROWS_PER_SYMBOL:
        if candidate.weekday() < 5:
            result.append(candidate)
        candidate = date.fromordinal(candidate.toordinal() - 1)
    return list(reversed(result))


def _synthetic_rows() -> Iterator[tuple[object, ...]]:
    """Yield the fixed, synthetic stock_daily payload in primary-key order."""
    dates = _synthetic_dates()
    for symbol_index in range(SYMBOL_COUNT):
        symbol = f"SYN{symbol_index:04d}"
        for row_index, trading_date in enumerate(dates):
            close_cents = 10_000 + symbol_index * 23 + row_index * 3
            volume = 1_000 + ((symbol_index * 31 + row_index * 17 + FIXTURE_SEED) % 900_000)
            close_price = close_cents / 100.0
            open_price = (close_cents - 12) / 100.0
            high_price = (close_cents + 25) / 100.0
            low_price = (close_cents - 35) / 100.0
            date_ms = int(
                datetime.combine(trading_date, datetime_time.min, tzinfo=timezone.utc).timestamp()
                * 1000
            )
            yield (
                symbol,
                trading_date,
                date_ms,
                "CNY",
                "1d",
                "none",
                open_price,
                high_price,
                low_price,
                close_price,
                float(volume),
                round(close_price * volume, 2),
            )


def synthetic_payload_sha256() -> str:
    """Hash the complete fixture using canonical row encoding."""
    digest = hashlib.sha256()
    for row in _synthetic_rows():
        normalized = [value.isoformat() if isinstance(value, date) else value for value in row]
        digest.update(_canonical_json(normalized))
        digest.update(b"\n")
    return digest.hexdigest()


def _tree_sha256(root: Path) -> str:
    """Hash only included regular source files by relative path, mode, and contents."""
    digest = hashlib.sha256()
    files, _excluded = _source_inventory(root)
    for path in files:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(relative)
        digest.update(b"\0")
        digest.update(f"{stat.S_IMODE(path.stat().st_mode):04o}".encode("ascii"))
        digest.update(b"\0")
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def _pinned_source_snapshot(repository: Path, destination: Path) -> tuple[Path, Path]:
    """Create HEAD/working package snapshots with the same frozen runner and config."""
    live_package = repository / "opendata"
    live_runner = repository / "scripts/quality/iter2_performance_probe.py"
    live_project_file = repository / "pyproject.toml"
    before_hash = _tree_sha256(live_package)
    runner_hash = _sha256_file(live_runner)
    project_hash = _sha256_file(live_project_file)
    current_root = destination / "current"
    current_root.mkdir(parents=True, exist_ok=False)
    _copy_source_package(live_package, current_root / "opendata")
    current_runner = current_root / "scripts/quality/iter2_performance_probe.py"
    current_runner.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(live_runner, current_runner)
    shutil.copy2(live_project_file, current_root / "pyproject.toml")
    copied_hash = _tree_sha256(current_root / "opendata")
    after_copy_hash = _tree_sha256(live_package)
    if (
        before_hash != copied_hash
        or before_hash != after_copy_hash
        or _sha256_file(current_runner) != runner_hash
        or _sha256_file(live_runner) != runner_hash
        or _sha256_file(current_root / "pyproject.toml") != project_hash
        or _sha256_file(live_project_file) != project_hash
    ):
        raise RuntimeError("working source changed while the current snapshot was copied")

    baseline_root = destination / "before"
    baseline_root.mkdir()
    archive_path = destination / "head-input-source.tar"
    archive = subprocess.run(  # noqa: S603 -- fixed git archive argv for pinned commit # nosec B603
        [
            _executable("git"),
            "archive",
            "--format=tar",
            BASELINE_COMMIT,
            "opendata",
            "pyproject.toml",
        ],
        cwd=repository,
        capture_output=True,
        check=True,
    ).stdout
    archive_path.write_bytes(archive)
    with tarfile.open(archive_path, "r:") as tar:
        for member in tar.getmembers():
            member_path = Path(member.name)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise RuntimeError("baseline archive contains an unsafe path")
            destination_path = baseline_root / member_path
            if member.isdir():
                destination_path.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                source = tar.extractfile(member)
                if source is None:
                    raise RuntimeError("baseline archive member could not be read")
                destination_path.parent.mkdir(parents=True, exist_ok=True)
                destination_path.write_bytes(source.read())
            else:
                raise RuntimeError("baseline archive contains a non-regular member")
    archive_path.unlink()
    baseline_runner = baseline_root / "scripts/quality/iter2_performance_probe.py"
    baseline_runner.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(live_runner, baseline_runner)
    if not (baseline_root / "opendata").is_dir():
        raise RuntimeError("baseline archive did not contain opendata/")
    if (
        _sha256_file(baseline_runner) != runner_hash
        or _sha256_file(current_runner) != runner_hash
        or _sha256_file(baseline_root / "pyproject.toml")
        != _sha256_file(current_root / "pyproject.toml")
    ):
        raise RuntimeError("source snapshots do not share the same frozen runner/config")
    return baseline_root, current_root


def _persist_current_source_archive(
    source_root: Path,
    run_dir: Path,
    run_id: str,
    run_suffix: str,
) -> dict[str, object]:
    """Persist a reproducible package-only archive for compatibility callers/tests."""
    if not re.fullmatch(r"[a-f0-9]{12}", run_suffix):
        raise ValueError("source archive suffix must be 12 lowercase hex characters")
    archive_path = run_dir / f"source-after-{run_suffix}.tar"
    manifest_path = run_dir / f"source-after-{run_suffix}.manifest.json"
    if archive_path.exists() or manifest_path.exists():
        raise FileExistsError("refusing to overwrite an existing after-source artifact")

    source_hash_before = _tree_sha256(source_root)
    files, excluded = _source_inventory(source_root)
    entries: list[_SourceFileEntry] = [
        {
            "path": path.relative_to(source_root).as_posix(),
            "size_bytes": path.stat().st_size,
            "mode_octal": f"{stat.S_IMODE(path.stat().st_mode):04o}",
            "sha256": _sha256_file(path),
        }
        for path in files
    ]
    try:
        with tarfile.open(archive_path, mode="x", format=tarfile.PAX_FORMAT) as archive:
            for path, entry in zip(files, entries, strict=True):
                info = tarfile.TarInfo(f"opendata/{entry['path']}")
                info.size = entry["size_bytes"]
                info.mode = stat.S_IMODE(path.stat().st_mode)
                info.mtime = 0
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                with path.open("rb") as source:
                    archive.addfile(info, source)
        source_hash_after = _tree_sha256(source_root)
        if source_hash_before != source_hash_after:
            raise RuntimeError("current source changed while its persistent archive was written")
        archive_hash = _sha256_file(archive_path)
        artifact_manifest: dict[str, object] = {
            "schema_version": 1,
            "role": "after",
            "run_id": run_id,
            "source_tree_sha256": source_hash_before,
            "archive_file": archive_path.name,
            "archive_sha256": archive_hash,
            "included_file_count": len(entries),
            "included_bytes": sum(entry["size_bytes"] for entry in entries),
            "excluded_entry_counts_by_reason": dict(sorted(excluded.items())),
            "files": entries,
        }
        _atomic_write_json(manifest_path, artifact_manifest)
    except Exception:
        with suppress(FileNotFoundError):
            archive_path.unlink()
        with suppress(FileNotFoundError):
            manifest_path.unlink()
        raise
    return {
        "archive_file": archive_path.name,
        "archive_sha256": archive_hash,
        "manifest_file": manifest_path.name,
        "manifest_sha256": _sha256_file(manifest_path),
        "source_tree_sha256": source_hash_before,
        "included_file_count": len(entries),
        "included_bytes": sum(entry["size_bytes"] for entry in entries),
    }


def _persist_source_bundle(
    source_root: Path,
    run_dir: Path,
    run_id: str,
    role: str,
    run_suffix: str,
) -> dict[str, object]:
    """Persist package, runner, and project config as one exact source input bundle."""
    if not re.fullmatch(r"[a-f0-9]{12}", run_suffix):
        raise ValueError("source archive suffix must be 12 lowercase hex characters")
    if role not in {"before", "after"}:
        raise ValueError("source bundle role must be before or after")
    if not (source_root / "opendata").is_dir():
        raise FileNotFoundError("source bundle root must contain opendata/")
    runner_path = source_root / "scripts/quality/iter2_performance_probe.py"
    project_path = source_root / "pyproject.toml"
    if not runner_path.is_file() or not project_path.is_file():
        raise FileNotFoundError("source bundle is missing its runner or project config")
    archive_path = run_dir / f"source-{role}-{run_suffix}.tar"
    manifest_path = run_dir / f"source-{role}-{run_suffix}.manifest.json"
    if archive_path.exists() or manifest_path.exists():
        raise FileExistsError("refusing to overwrite an existing source bundle artifact")

    package_root = source_root / "opendata"
    source_hash_before = _tree_sha256(package_root)
    runner_hash_before = _sha256_file(runner_path)
    project_hash_before = _sha256_file(project_path)
    files, excluded = _source_inventory(package_root)
    entries: list[_SourceFileEntry] = [
        {
            "path": f"opendata/{path.relative_to(package_root).as_posix()}",
            "size_bytes": path.stat().st_size,
            "mode_octal": f"{stat.S_IMODE(path.stat().st_mode):04o}",
            "sha256": _sha256_file(path),
        }
        for path in files
    ]
    for path, archive_name in (
        (runner_path, "scripts/quality/iter2_performance_probe.py"),
        (project_path, "pyproject.toml"),
    ):
        entries.append(
            {
                "path": archive_name,
                "size_bytes": path.stat().st_size,
                "mode_octal": f"{stat.S_IMODE(path.stat().st_mode):04o}",
                "sha256": _sha256_file(path),
            }
        )
    entries.sort(key=lambda entry: str(entry["path"]))
    try:
        with tarfile.open(archive_path, mode="x", format=tarfile.PAX_FORMAT) as archive:
            for entry in entries:
                archive_name = str(entry["path"])
                source_path = source_root / archive_name
                info = tarfile.TarInfo(archive_name)
                info.size = entry["size_bytes"]
                info.mode = stat.S_IMODE(source_path.stat().st_mode)
                info.mtime = 0
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                with source_path.open("rb") as source:
                    archive.addfile(info, source)
        if (
            source_hash_before != _tree_sha256(package_root)
            or runner_hash_before != _sha256_file(runner_path)
            or project_hash_before != _sha256_file(project_path)
        ):
            raise RuntimeError("source changed while its persistent bundle was written")
        archive_hash = _sha256_file(archive_path)
        artifact_manifest: dict[str, object] = {
            "schema_version": 1,
            "role": role,
            "run_id": run_id,
            "source_package_tree_sha256": source_hash_before,
            "runner_sha256": runner_hash_before,
            "pyproject_sha256": project_hash_before,
            "archive_file": archive_path.name,
            "archive_sha256": archive_hash,
            "included_file_count": len(entries),
            "included_bytes": sum(entry["size_bytes"] for entry in entries),
            "excluded_entry_counts_by_reason": dict(sorted(excluded.items())),
            "files": entries,
        }
        _atomic_write_json(manifest_path, artifact_manifest)
    except Exception:
        with suppress(FileNotFoundError):
            archive_path.unlink()
        with suppress(FileNotFoundError):
            manifest_path.unlink()
        raise
    return {
        "archive_file": archive_path.name,
        "archive_sha256": archive_hash,
        "manifest_file": manifest_path.name,
        "manifest_sha256": _sha256_file(manifest_path),
        "source_package_tree_sha256": source_hash_before,
        "runner_sha256": runner_hash_before,
        "pyproject_sha256": project_hash_before,
        "included_file_count": len(entries),
        "included_bytes": sum(entry["size_bytes"] for entry in entries),
    }


def _extract_verified_source_bundle(
    archive_path: Path,
    manifest_path: Path,
    destination: Path,
) -> Path:
    """Verify a retained source bundle, extract it safely, then make it read-only."""
    if destination.exists():
        raise FileExistsError("refusing to overwrite an extracted source bundle")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("archive_sha256") != _sha256_file(
        archive_path
    ):
        raise RuntimeError("source bundle archive hash does not match its manifest")
    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        raise RuntimeError("source bundle manifest has no file inventory")
    expected_files = {
        str(entry.get("path")): entry
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("path"), str)
    }
    if len(expected_files) != len(entries):
        raise RuntimeError("source bundle manifest contains duplicate or malformed paths")
    destination.mkdir(parents=True, exist_ok=False)
    try:
        with tarfile.open(archive_path, mode="r:") as archive:
            members = archive.getmembers()
            if {member.name for member in members if member.isfile()} != set(expected_files):
                raise RuntimeError("source bundle members differ from the exact manifest inventory")
            for member in members:
                relative = Path(member.name)
                if relative.is_absolute() or ".." in relative.parts or not member.isfile():
                    raise RuntimeError("source bundle contains an unsafe or non-regular member")
                entry = expected_files.get(member.name)
                if entry is None or member.size != entry.get("size_bytes"):
                    raise RuntimeError("source bundle member size differs from its manifest")
                source = archive.extractfile(member)
                if source is None:
                    raise RuntimeError("source bundle member could not be read")
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read())
                if _sha256_file(target) != entry.get("sha256"):
                    raise RuntimeError("source bundle member hash differs from its manifest")
                recorded_mode = int(str(entry.get("mode_octal")), 8)
                if member.mode != recorded_mode:
                    raise RuntimeError("source bundle member mode differs from its manifest")
                target.chmod(recorded_mode)
        if _tree_sha256(destination / "opendata") != manifest.get("source_package_tree_sha256"):
            raise RuntimeError("extracted package tree differs from its manifest")
        if _sha256_file(destination / "scripts/quality/iter2_performance_probe.py") != manifest.get(
            "runner_sha256"
        ):
            raise RuntimeError("extracted runner differs from its manifest")
        if _sha256_file(destination / "pyproject.toml") != manifest.get("pyproject_sha256"):
            raise RuntimeError("extracted project config differs from its manifest")
        for path in destination.rglob("*"):
            if path.is_file():
                path.chmod(stat.S_IMODE(path.stat().st_mode) & ~0o222)
        directories = sorted(
            (path for path in destination.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        )
        for directory in directories:
            directory.chmod(0o555)
        destination.chmod(0o555)
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return destination


def _verify_staged_source_bundle(
    archive_path: Path, manifest_path: Path, staging_root: Path
) -> dict[str, object]:
    """Recheck every retained archive byte and readonly staging mode before cleanup."""
    manifest_hash = _sha256_file(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    archive_hash = _sha256_file(archive_path)
    if not isinstance(manifest, dict) or manifest.get("archive_sha256") != archive_hash:
        raise RuntimeError("retained source bundle no longer matches its manifest")
    raw_entries = manifest.get("files")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise RuntimeError("retained source bundle inventory is missing")
    entries = [_parse_source_file_entry(entry) for entry in raw_entries]
    expected = {entry["path"]: entry for entry in entries}
    if len(expected) != len(entries):
        raise RuntimeError("retained source bundle inventory is malformed")
    actual = {
        path.relative_to(staging_root).as_posix()
        for path in staging_root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    if actual != set(expected):
        raise RuntimeError("readonly source staging path inventory changed")
    file_evidence: list[_VerifiedSourceFileEntry] = []
    with tarfile.open(archive_path, mode="r:") as archive:
        members = {member.name: member for member in archive.getmembers()}
        if set(members) != set(expected) or not all(member.isfile() for member in members.values()):
            raise RuntimeError("retained source tar member inventory changed")
        for name, entry in sorted(expected.items()):
            if Path(name).is_absolute() or ".." in Path(name).parts:
                raise RuntimeError("retained source bundle contains an unsafe path")
            path = staging_root / name
            member = members[name]
            source = archive.extractfile(member)
            if source is None:
                raise RuntimeError("retained source tar member could not be read")
            member_bytes = source.read()
            staged_hash = _sha256_file(path)
            archive_mode = int(str(entry.get("mode_octal")), 8)
            staging_mode = stat.S_IMODE(path.stat().st_mode)
            if (
                path.is_symlink()
                or path.stat().st_size != entry.get("size_bytes")
                or staged_hash != entry.get("sha256")
                or hashlib.sha256(member_bytes).hexdigest() != entry.get("sha256")
                or member.size != entry.get("size_bytes")
                or stat.S_IMODE(member.mode) != archive_mode
                or staging_mode != (archive_mode & ~0o222)
            ):
                raise RuntimeError(f"retained source staging bytes or mode changed: {name}")
            file_evidence.append(
                {
                    "path": name,
                    "sha256": staged_hash,
                    "size_bytes": entry["size_bytes"],
                    "archive_mode_octal": f"{archive_mode:04o}",
                    "staging_mode_octal": f"{staging_mode:04o}",
                }
            )
    directories = [staging_root, *(path for path in staging_root.rglob("*") if path.is_dir())]
    if any(path.is_symlink() or stat.S_IMODE(path.stat().st_mode) & 0o222 for path in directories):
        raise RuntimeError("readonly source staging directory permissions changed")
    return {
        "role": manifest.get("role"),
        "archive_file": archive_path.name,
        "archive_sha256": archive_hash,
        "manifest_file": manifest_path.name,
        "manifest_sha256": manifest_hash,
        "staging_directory": str(staging_root),
        "verified_file_count": len(file_evidence),
        "verified_file_bytes": sum(int(entry["size_bytes"]) for entry in file_evidence),
        "all_staging_bytes_match_archive_and_manifest": True,
        "original_archive_modes_match_manifest": True,
        "staging_modes_are_readonly_archive_mode_without_write_bits": True,
        "verified_files": file_evidence,
        "rebuild": {
            "extract": f"tar -xf {archive_path} -C <new-empty-directory>",
            "readonly_transform": (
                "for each file, chmod(original manifest mode & ~0222); then chmod all "
                "directories deepest-first and root to 0555"
            ),
            "authoritative_verifier": "_extract_verified_source_bundle",
        },
    }


def _schema_name(run_suffix: str, side: str) -> str:
    value = f"opendata_c100_{run_suffix}_{side}"
    if not _SAFE_SCHEMA.fullmatch(value):
        raise ValueError("generated schema name did not satisfy its task-only pattern")
    return value


def _is_valid_p95(value: object, samples: Sequence[object]) -> bool:
    """Check that a P95 is present, finite, non-negative, and matches its samples."""
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or value < 0
        or len(samples) != MEASURED_SAMPLES
    ):
        return False
    numeric_samples = [sample for sample in samples if isinstance(sample, (int, float))]
    if len(numeric_samples) != len(samples):
        return False
    try:
        expected = _p95([float(sample) for sample in numeric_samples])
    except (TypeError, ValueError):
        return False
    return float(value) == expected


def _valid_import_reading(entry: object) -> bool:
    """Require a complete cold/replay import record with stable target hash."""
    if not isinstance(entry, dict):
        return False
    kind = entry.get("kind")
    expected_before = 0 if kind == "cold" else EXPECTED_ROWS
    elapsed = entry.get("elapsed_seconds")
    return (
        kind in {"cold", "idempotent_replay_1", "idempotent_replay_2"}
        and entry.get("rows_read") == EXPECTED_ROWS
        and entry.get("rows_written") == EXPECTED_ROWS
        and entry.get("target_rows_before") == expected_before
        and entry.get("target_rows_after") == EXPECTED_ROWS
        and isinstance(entry.get("batches_written"), int)
        and entry["batches_written"] > 0
        and isinstance(elapsed, (int, float))
        and not isinstance(elapsed, bool)
        and math.isfinite(float(elapsed))
        and elapsed > 0
        and isinstance(entry.get("target_business_sha256"), str)
        and bool(entry.get("target_business_sha256"))
        and entry.get("source_readonly_verified") is True
        and isinstance(entry.get("process_peak_rss_bytes"), int)
        and entry.get("process_peak_rss_bytes", 0) > 0
    )


def _valid_connection_counter(value: object) -> bool:
    """Validate SQLAlchemy physical-connection and pool-use counters."""
    return isinstance(value, dict) and all(
        isinstance(value.get(name), int)
        and not isinstance(value.get(name), bool)
        and value[name] >= 0
        for name in ("connect", "checkout", "checkin")
    )


def _valid_api_route_adaptation(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    passed = value.get("optional_none_arguments_passed")
    omitted = value.get("optional_none_arguments_omitted")
    if (
        value.get("route") != "query_domain_data"
        or value.get("inspected_at_runtime") is not True
        or not isinstance(passed, list)
        or not isinstance(omitted, list)
        or any(not isinstance(item, str) for item in passed + omitted)
    ):
        return False
    return not (set(passed) & set(omitted)) and (set(passed) | set(omitted)) == {
        "as_of",
        "market",
    }


def _optional_none_signature_arguments(
    route: Callable[..., object], optional_names: Sequence[str]
) -> tuple[dict[str, None], list[str], list[str]]:
    """Pass only optional ``None`` keywords supported by the inspected route."""
    parameters = inspect_module.signature(route).parameters
    supported = [name for name in optional_names if name in parameters]
    omitted = [name for name in optional_names if name not in parameters]
    return dict.fromkeys(supported, None), supported, omitted


def _validate_comparison(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Check exact-input/schema/measurement coverage and expose output differences."""
    issues: list[str] = []
    if not isinstance(before.get("payload_sha256"), str) or not isinstance(
        after.get("payload_sha256"), str
    ):
        issues.append("input_payload_missing")
    elif before["payload_sha256"] != after["payload_sha256"]:
        issues.append("input_payload_mismatch")
    if not isinstance(before.get("ddl_sha256"), str) or not isinstance(
        after.get("ddl_sha256"), str
    ):
        issues.append("ddl_missing")
    elif before["ddl_sha256"] != after["ddl_sha256"]:
        issues.append("ddl_mismatch")
    if not isinstance(before.get("fixture_config"), dict) or not isinstance(
        after.get("fixture_config"), dict
    ):
        issues.append("fixture_config_missing")
    elif before["fixture_config"] != after["fixture_config"]:
        issues.append("fixture_config_mismatch")
    if not isinstance(before.get("schema_shape"), dict) or not isinstance(
        after.get("schema_shape"), dict
    ):
        issues.append("schema_shape_missing")
    elif before["schema_shape"] != after["schema_shape"]:
        issues.append("schema_shape_mismatch")
    for side_name, snapshot in (("before", before), ("after", after)):
        shape = snapshot.get("schema_shape")
        if (
            not isinstance(shape, dict)
            or not isinstance(shape.get("source"), dict)
            or not isinstance(shape.get("target"), dict)
            or shape.get("source") != shape.get("target")
            or shape.get("source_equals_target") is not True
            or shape.get("source_ddl_sha256") != shape.get("target_ddl_sha256")
            or not isinstance(shape.get("source", {}).get("columns"), list)
            or not shape.get("source", {}).get("columns")
            or shape.get("source", {}).get("primary_key") != list(KEY)
            or not isinstance(shape.get("source_ddl_sha256"), str)
        ):
            issues.append(f"{side_name}_schema_inconsistent")

    family_comparison: dict[str, Any] = {}
    before_measurements = before.get("measurements")
    after_measurements = after.get("measurements")
    for family in ("query", "export"):
        left = before_measurements.get(family) if isinstance(before_measurements, dict) else None
        right = after_measurements.get(family) if isinstance(after_measurements, dict) else None
        if not isinstance(left, dict) or not isinstance(right, dict):
            issues.append(f"missing_{family}_metrics")
            family_comparison[family] = {"results_equal": False, "metrics_complete": False}
            continue
        left_samples = left.get("samples_seconds")
        right_samples = right.get("samples_seconds")
        if (
            not isinstance(left_samples, list)
            or not isinstance(right_samples, list)
            or len(left_samples) != MEASURED_SAMPLES
            or len(right_samples) != MEASURED_SAMPLES
            or left.get("warmup_count") != WARMUPS
            or right.get("warmup_count") != WARMUPS
            or any(
                not isinstance(item, (int, float)) or not math.isfinite(item)
                for item in left_samples + right_samples
            )
            or not _is_valid_p95(left.get("p95_seconds"), left_samples)
            or not _is_valid_p95(right.get("p95_seconds"), right_samples)
            or left.get("result_row_count")
            != (QUERY_PAGE_SIZE if family == "query" else EXPORT_LIMIT)
            or right.get("result_row_count")
            != (QUERY_PAGE_SIZE if family == "query" else EXPORT_LIMIT)
            or not isinstance(left.get("result_sha256"), str)
            or not left.get("result_sha256")
            or not isinstance(right.get("result_sha256"), str)
            or not right.get("result_sha256")
            or not isinstance(left.get("process_peak_rss_max_bytes"), int)
            or left.get("process_peak_rss_max_bytes", 0) <= 0
            or not isinstance(right.get("process_peak_rss_max_bytes"), int)
            or right.get("process_peak_rss_max_bytes", 0) <= 0
            or not _valid_connection_counter(left.get("connection_counts"))
            or not _valid_connection_counter(right.get("connection_counts"))
        ):
            issues.append(f"incomplete_{family}_metrics")
            complete = False
        else:
            complete = True
        left_hash = left.get("result_sha256")
        right_hash = right.get("result_sha256")
        equal = isinstance(left_hash, str) and left_hash == right_hash
        if not equal:
            issues.append(f"{family}_result_hash_diff")
        family_comparison[family] = {
            "results_equal": equal,
            "metrics_complete": complete,
            "before_result_sha256": left_hash,
            "after_result_sha256": right_hash,
        }
    for side_name, snapshot in (("before", before), ("after", after)):
        imports = snapshot.get("import_runs")
        if not isinstance(imports, list) or len(imports) != 3:
            issues.append(f"incomplete_{side_name}_import_runs")
            continue
        if [entry.get("kind") for entry in imports if isinstance(entry, dict)] != [
            "cold",
            "idempotent_replay_1",
            "idempotent_replay_2",
        ]:
            issues.append(f"invalid_{side_name}_import_run_order")
        if any(not _valid_import_reading(entry) for entry in imports):
            issues.append(f"incomplete_{side_name}_import_readings")
        elif len({entry.get("target_business_sha256") for entry in imports}) != 1:
            issues.append(f"{side_name}_idempotent_result_hash_diff")
    imports_before = before.get("import_runs")
    imports_after = after.get("import_runs")
    import_hashes_equal = (
        isinstance(imports_before, list)
        and isinstance(imports_after, list)
        and len(imports_before) == len(imports_after) == 3
        and all(
            isinstance(left, dict)
            and isinstance(right, dict)
            and left.get("target_business_sha256") == right.get("target_business_sha256")
            for left, right in zip(imports_before, imports_after, strict=True)
        )
    )
    if not import_hashes_equal:
        issues.append("import_result_hash_diff")
    if (
        before.get("source_readonly_verified") is not True
        or after.get("source_readonly_verified") is not True
    ):
        issues.append("source_readonly_not_verified")
    for side_name, snapshot in (("before", before), ("after", after)):
        frozen_input = snapshot.get("frozen_input")
        if (
            not isinstance(frozen_input, dict)
            or not isinstance(frozen_input.get("archive_root"), str)
            or frozen_input.get("extracted_package_tree_sha256")
            != snapshot.get("source_tree_sha256")
            or not isinstance(frozen_input.get("source_package_archive_tree_sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", frozen_input["source_package_archive_tree_sha256"])
            or not isinstance(frozen_input.get("source_bundle_archive_sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", frozen_input["source_bundle_archive_sha256"])
            or not isinstance(frozen_input.get("source_bundle_manifest_sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", frozen_input["source_bundle_manifest_sha256"])
            or not isinstance(frozen_input.get("runner_sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", frozen_input["runner_sha256"])
            or not isinstance(frozen_input.get("pyproject_sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", frozen_input["pyproject_sha256"])
        ):
            issues.append(f"invalid_{side_name}_frozen_input")
        counts = snapshot.get("connection_counts")
        if not isinstance(counts, dict) or any(
            not _valid_connection_counter(counts.get(engine)) for engine in ("source", "target")
        ):
            issues.append(f"{side_name}_connection_counts_missing")
        import_counts = snapshot.get("import_connection_counts")
        if not isinstance(import_counts, dict) or any(
            not _valid_connection_counter(import_counts.get(engine))
            for engine in ("source", "target")
        ):
            issues.append(f"{side_name}_import_connection_counts_missing")
        if (
            snapshot.get("source_rows") != EXPECTED_ROWS
            or snapshot.get("target_rows") != EXPECTED_ROWS
            or not isinstance(snapshot.get("process_peak_rss_bytes"), int)
            or snapshot.get("process_peak_rss_bytes", 0) <= 0
        ):
            issues.append(f"incomplete_{side_name}_row_or_rss_readings")
        if not _valid_api_route_adaptation(snapshot.get("api_route_signature_adaptation")):
            issues.append(f"invalid_{side_name}_api_route_signature_adaptation")
        socket_proof = snapshot.get("connection_guard")
        label = snapshot.get("label")
        if not isinstance(socket_proof, dict):
            issues.append(f"{side_name}_connection_guard_missing")
        else:
            try:
                _parse_loopback_port(str(socket_proof.get("allowed_destination", "")))
                schemas = socket_proof.get("schemas")
                if not isinstance(schemas, dict):
                    raise ValueError("schema proof is missing")
                source_schema = schemas.get("source")
                target_schema = schemas.get("target")
                suffix_match = (
                    re.fullmatch(
                        rf"opendata_c100_([a-f0-9]{{12}})_{label}_source",
                        str(source_schema),
                    )
                    if label in {"before", "after"}
                    else None
                )
                container_name = str(socket_proof.get("container_name", ""))
                container_match = re.fullmatch(
                    r"opendata-c100-perf-\d{8}T\d{6}Z-([a-f0-9]{12})", container_name
                )
                expected_task_schemas = (
                    sorted(
                        _schema_name(suffix_match.group(1), f"{side}_{role}")
                        for side in ("before", "after")
                        for role in ("source", "target")
                    )
                    if suffix_match is not None
                    else None
                )
                if (
                    not isinstance(socket_proof.get("allowed_connection_attempts"), int)
                    or isinstance(socket_proof.get("allowed_connection_attempts"), bool)
                    or socket_proof["allowed_connection_attempts"] <= 0
                    or socket_proof.get("blocked_attempts") != 0
                    or not isinstance(socket_proof.get("container_id"), str)
                    or not re.fullmatch(r"[a-f0-9]{64}", socket_proof["container_id"])
                    or not isinstance(socket_proof.get("docker_endpoint"), str)
                    or not socket_proof["docker_endpoint"].startswith("unix://")
                    or not isinstance(socket_proof.get("volume_name"), str)
                    or suffix_match is None
                    or target_schema != _schema_name(suffix_match.group(1), f"{label}_target")
                    or socket_proof.get("sql_guard_schemas")
                    != {"source": source_schema, "target": target_schema}
                    or socket_proof["volume_name"] != f"opendata-c100-perf-{suffix_match.group(1)}"
                    or container_match is None
                    or container_match.group(1) != suffix_match.group(1)
                    or socket_proof.get("verified_task_schemas") != expected_task_schemas
                ):
                    raise ValueError("guard proof fields do not match the worker schemas")
            except (TypeError, ValueError):
                issues.append(f"invalid_{side_name}_connection_guard")
        process_proof = snapshot.get("process_guard")
        if not isinstance(process_proof, dict):
            issues.append(f"{side_name}_process_guard_missing")
        elif (
            process_proof.get("mode") != "worker_process_guard"
            or process_proof.get("blocked_attempts") != 0
            or not isinstance(process_proof.get("fixed_ps_attempts"), int)
            or isinstance(process_proof.get("fixed_ps_attempts"), bool)
            or process_proof.get("fixed_ps_attempts", 0) <= 0
            or not isinstance(process_proof.get("installed_hooks"), list)
            or not any("Popen.__init__" in hook for hook in process_proof["installed_hooks"])
            or process_proof.get("native_syscall_boundary")
            != "Python entry points only; arbitrary native syscalls are not proven blocked"
        ):
            issues.append(f"invalid_{side_name}_process_guard")
        http_proof = snapshot.get("http_guard")
        if not isinstance(http_proof, dict):
            issues.append(f"{side_name}_http_guard_missing")
        elif (
            http_proof.get("mode") != "deny_all_python_http_dispatch"
            or http_proof.get("blocked_attempts") != 0
            or not isinstance(http_proof.get("installed_hooks"), list)
            or "urllib.request.urlopen" not in http_proof["installed_hooks"]
            or "http.client.HTTPConnection.connect" not in http_proof["installed_hooks"]
            or not isinstance(http_proof.get("blocked_reasons"), dict)
            or http_proof.get("blocked_reasons")
        ):
            issues.append(f"invalid_{side_name}_http_guard")
        explain = snapshot.get("explain")
        if not isinstance(explain, dict) or any(
            not isinstance(explain.get(name), str) or not explain.get(name)
            for name in (
                "query_plan_sha256",
                "export_plan_sha256",
                "query_sql_sha256",
                "export_sql_sha256",
            )
        ):
            issues.append(f"{side_name}_explain_missing")
    frozen_before = before.get("frozen_input")
    frozen_after = after.get("frozen_input")
    if (
        isinstance(frozen_before, dict)
        and isinstance(frozen_after, dict)
        and any(
            frozen_before.get(field) != frozen_after.get(field)
            for field in ("runner_sha256", "pyproject_sha256")
        )
    ):
        issues.append("shared_frozen_runner_or_config_mismatch")
    before_guard = before.get("connection_guard")
    after_guard = after.get("connection_guard")
    if isinstance(before_guard, dict) and isinstance(after_guard, dict):
        if any(
            before_guard.get(field) != after_guard.get(field)
            for field in (
                "allowed_destination",
                "docker_endpoint",
                "container_id",
                "container_name",
                "volume_name",
            )
        ):
            issues.append("worker_container_identity_mismatch")
        before_schemas = before_guard.get("schemas")
        after_schemas = after_guard.get("schemas")
        if (
            not isinstance(before_schemas, dict)
            or not isinstance(after_schemas, dict)
            or before_schemas.get("source") == after_schemas.get("source")
            or before_schemas.get("target") == after_schemas.get("target")
            or not isinstance(before_schemas.get("source"), str)
            or not isinstance(after_schemas.get("source"), str)
            or before_schemas["source"].split("_")[2] != after_schemas["source"].split("_")[2]
        ):
            issues.append("worker_schema_side_mismatch")
    return {
        "comparable": not any(
            issue
            for issue in issues
            if issue
            in {
                "input_payload_mismatch",
                "input_payload_missing",
                "ddl_mismatch",
                "ddl_missing",
                "fixture_config_mismatch",
                "fixture_config_missing",
                "schema_shape_mismatch",
                "schema_shape_missing",
                "source_readonly_not_verified",
            }
            or issue.endswith("_schema_inconsistent")
            or issue.startswith("missing_")
            or issue.endswith("_connection_counts_missing")
            or issue.endswith("_import_connection_counts_missing")
            or issue.endswith("_explain_missing")
            or issue.startswith("incomplete_")
            or issue.startswith("invalid_")
            or issue.endswith("_connection_guard_missing")
            or issue.endswith("_connection_guard_invalid")
            or issue.endswith("_process_guard_missing")
            or issue.endswith("_http_guard_missing")
            or issue.endswith("_process_guard")
            or issue.endswith("_http_guard")
            or issue
            in {
                "worker_container_identity_mismatch",
                "worker_schema_side_mismatch",
                "shared_frozen_runner_or_config_mismatch",
            }
        ),
        "issues": issues,
        "families": {
            **family_comparison,
            "imports": {
                "results_equal": import_hashes_equal,
                "metrics_complete": import_hashes_equal,
            },
        },
        "results_equal": import_hashes_equal
        and all(item["results_equal"] for item in family_comparison.values()),
        "p95_seconds": {
            family: {
                "before": (
                    before_measurements.get(family, {}).get("p95_seconds")
                    if isinstance(before_measurements, dict)
                    and isinstance(before_measurements.get(family), dict)
                    else None
                ),
                "after": (
                    after_measurements.get(family, {}).get("p95_seconds")
                    if isinstance(after_measurements, dict)
                    and isinstance(after_measurements.get(family), dict)
                    else None
                ),
            }
            for family in ("query", "export")
        },
    }


def _run_command(
    args: Sequence[str], *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run one literal argv command and retain only text output."""
    return subprocess.run(  # noqa: S603 -- internal callers use fixed/validated argv # nosec B603
        list(args),
        capture_output=True,
        check=False,
        text=True,
        timeout=180,
        env=env,
    )


def _docker(args: Sequence[str], *, env: dict[str, str] | None = None, check: bool = True) -> str:
    result = _run_command([_executable("docker"), *args], env=env)
    if check and result.returncode != 0:
        raise RuntimeError(f"docker command failed ({result.returncode}): {' '.join(args[:3])}")
    return result.stdout.strip()


def _docker_resource_exists(resource_kind: str, name: str) -> bool:
    """Inspect one generated name, distinguishing absence from Docker failures."""
    if resource_kind == "container":
        if re.fullmatch(r"opendata-c100-perf-\d{8}T\d{6}Z-[a-f0-9]{12}", name) is None:
            raise ValueError("container probe name is outside the task naming scheme")
        arguments = ["inspect", name]
        expected_name = f"/{name}"
        not_found_messages = {
            f"Error: No such object: {name}",
            f"Error response from daemon: No such object: {name}",
        }
    elif resource_kind == "volume":
        if re.fullmatch(r"opendata-c100-perf-[a-f0-9]{12}", name) is None:
            raise ValueError("volume probe name is outside the task naming scheme")
        arguments = ["volume", "inspect", name]
        expected_name = name
        not_found_messages = {
            f"Error response from daemon: get {name}: no such volume",
            f"Error: get {name}: no such volume",
        }
    else:
        raise ValueError("Docker resource probe supports only task containers and volumes")

    result = _run_command([_executable("docker"), *arguments])
    stdout = result.stdout.strip()
    stderr = result.stderr.strip()
    if result.returncode == 0:
        try:
            inspected = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Docker {resource_kind} inspect returned invalid JSON") from exc
        if (
            not isinstance(inspected, list)
            or len(inspected) != 1
            or not isinstance(inspected[0], dict)
            or inspected[0].get("Name") != expected_name
        ):
            raise RuntimeError(f"Docker {resource_kind} inspect returned an unexpected identity")
        return True
    if result.returncode == 1 and stdout == "[]" and stderr in not_found_messages:
        return False
    raise RuntimeError(
        f"unable to verify task Docker {resource_kind} name (inspect exit {result.returncode})"
    )


def _require_local_docker_endpoint() -> str:
    """Reject remote Docker contexts before inspecting or mutating resources."""
    override = os.environ.get("DOCKER_HOST")
    endpoint = override or _docker(["context", "inspect", "--format", "{{.Endpoints.docker.Host}}"])
    if not endpoint.startswith("unix://"):
        raise RuntimeError("performance probe requires a local Unix-socket Docker endpoint")
    return endpoint


def _executable(name: str) -> str:
    """Resolve a required executable to an absolute path before subprocess use."""
    path = shutil.which(name)
    if path is None:
        raise FileNotFoundError(f"required executable is unavailable: {name}")
    return path


def _parse_loopback_port(value: str) -> int:
    match = re.fullmatch(r"127\.0\.0\.1:(\d+)", value.strip())
    if match is None:
        raise ValueError("published MySQL endpoint must be exactly 127.0.0.1:<port>")
    port = int(match.group(1))
    if not 1 <= port <= 65535 or port in {3306, 33565}:
        raise ValueError("published MySQL port is invalid or conflicts with protected ports")
    return port


class _TaskSocketGuard:
    """Permit only the verified task MySQL endpoint before app imports."""

    def __init__(self, port: int) -> None:
        if not 1 <= port <= 65535 or port in {3306, 33565}:
            raise ValueError("worker port is invalid or protected")
        self.port = port
        self.allowed_connection_attempts = 0
        self.blocked_attempts = 0
        self.blocked_reasons: dict[str, int] = {}
        self._originals: dict[str, object] = {}

    def _deny(self, reason: str) -> NoReturn:
        self.blocked_attempts += 1
        self.blocked_reasons[reason] = self.blocked_reasons.get(reason, 0) + 1
        raise PermissionError(f"isolated worker socket guard blocked {reason}")

    def _authorize(self, family: int, address: object, socket_type: int | None = None) -> None:
        if family != socket.AF_INET:
            self._deny("non_ipv4_destination")
        if socket_type is not None and socket_type & 0xF != socket.SOCK_STREAM:
            self._deny("non_tcp_destination")
        if (
            not isinstance(address, tuple)
            or len(address) < 2
            or address[0] != "127.0.0.1"
            or isinstance(address[1], bool)
            or not isinstance(address[1], int)
            or address[1] != self.port
        ):
            self._deny("non_task_destination")

    def _authorize_host_port(self, host: object, port: object) -> None:
        if (
            host != "127.0.0.1"
            or isinstance(port, bool)
            or not isinstance(port, (str, int))
            or str(port) != str(self.port)
        ):
            self._deny("non_task_name_resolution")

    def call_connect(
        self,
        original: Callable[..., object],
        family: int,
        sock: object,
        address: object,
        socket_type: int | None = None,
    ) -> object:
        """Authorize before calling an injected/original socket connect method."""
        self._authorize(family, address, socket_type)
        self.allowed_connection_attempts += 1
        return original(sock, address)

    def call_getaddrinfo(
        self,
        original: Callable[..., object],
        host: object,
        port: object,
        *args: object,
        **kwargs: object,
    ) -> object:
        self._authorize_host_port(host, port)
        if args and args[0] not in (0, socket.AF_INET):
            self._deny("non_ipv4_name_resolution")
        if kwargs.get("family", 0) not in (0, socket.AF_INET):
            self._deny("non_ipv4_name_resolution")
        return original(host, port, *args, **kwargs)

    def call_create_connection(
        self, original: Callable[..., object], address: object, *args: object, **kwargs: object
    ) -> object:
        self._authorize(socket.AF_INET, address, socket.SOCK_STREAM)
        return original(address, *args, **kwargs)

    def call_sendto(
        self, original: Callable[..., object], sock: object, *args: object, **kwargs: object
    ) -> NoReturn:
        self._deny("datagram_send")

    def install(self) -> None:
        """Patch outbound socket entry points without interfering with socketpair()."""
        if self._originals:
            raise RuntimeError("socket guard is already installed")

        original_connect = socket.socket.connect
        original_connect_ex = socket.socket.connect_ex
        original_create_connection = socket.create_connection
        original_getaddrinfo = socket.getaddrinfo
        original_sendto = socket.socket.sendto
        self._originals = {
            "connect": original_connect,
            "connect_ex": original_connect_ex,
            "create_connection": original_create_connection,
            "getaddrinfo": original_getaddrinfo,
            "sendto": original_sendto,
        }

        def guarded_connect(sock: socket.socket, address: object) -> object:
            return self.call_connect(original_connect, sock.family, sock, address, sock.type)

        def guarded_connect_ex(sock: socket.socket, address: object) -> object:
            return self.call_connect(original_connect_ex, sock.family, sock, address, sock.type)

        def guarded_create_connection(address: object, *args: object, **kwargs: object) -> object:
            return self.call_create_connection(original_create_connection, address, *args, **kwargs)

        def guarded_getaddrinfo(
            host: object, port: object, *args: object, **kwargs: object
        ) -> object:
            return self.call_getaddrinfo(original_getaddrinfo, host, port, *args, **kwargs)

        def guarded_sendto(sock: socket.socket, *args: object, **kwargs: object) -> object:
            return self.call_sendto(original_sendto, sock, *args, **kwargs)

        socket_replacements: dict[str, object] = {
            "connect": guarded_connect,
            "connect_ex": guarded_connect_ex,
            "sendto": guarded_sendto,
        }
        for name, replacement in socket_replacements.items():
            setattr(socket.socket, name, replacement)
        module_replacements: dict[str, object] = {
            "create_connection": guarded_create_connection,
            "getaddrinfo": guarded_getaddrinfo,
        }
        for name, replacement in module_replacements.items():
            setattr(socket, name, replacement)

    def restore(self) -> None:
        """Restore standard-library socket entry points after the worker exits."""
        if not self._originals:
            return
        for name in ("connect", "connect_ex", "sendto"):
            setattr(socket.socket, name, self._originals[name])
        for name in ("create_connection", "getaddrinfo"):
            setattr(socket, name, self._originals[name])
        self._originals.clear()

    def assert_no_blocked_attempts(self) -> None:
        if self.blocked_attempts:
            raise RuntimeError(
                "worker observed blocked socket attempts: "
                f"{json.dumps(self.blocked_reasons, sort_keys=True)}"
            )


class _TaskProcessGuard:
    """Deny Python process creation except the fixed absolute-path RSS probe."""

    _SAFE_POPEN_OPTIONS: dict[str, object] = {
        "bufsize": -1,
        "executable": None,
        "stdin": None,
        "stdout": -1,
        "stderr": -1,
        "preexec_fn": None,
        "close_fds": True,
        "shell": False,
        "cwd": None,
        "env": None,
        "universal_newlines": None,
        "startupinfo": None,
        "creationflags": 0,
        "restore_signals": True,
        "start_new_session": False,
        "pass_fds": (),
        "user": None,
        "group": None,
        "extra_groups": None,
        "encoding": None,
        "errors": None,
        "text": True,
        "umask": -1,
        "pipesize": -1,
        "process_group": None,
    }
    _OS_PROCESS_METHODS = {
        "fork",
        "forkpty",
        "popen",
        "startfile",
        "system",
        "posix_spawn",
        "posix_spawnp",
    }

    def __init__(self, *, allow_fixed_ps: bool = True) -> None:
        self.allow_fixed_ps = allow_fixed_ps
        self.fixed_ps_attempts = 0
        self.blocked_attempts = 0
        self.blocked_reasons: dict[str, int] = {}
        self.installed_hooks: list[str] = []
        self._thread_state = threading.local()
        self._patched: list[tuple[object, str, object]] = []

    def _deny(self, name: str) -> NoReturn:
        self.blocked_attempts += 1
        self.blocked_reasons[name] = self.blocked_reasons.get(name, 0) + 1
        raise PermissionError(f"isolated worker process guard blocked {name}")

    def _is_fixed_ps(self, args: object, kwargs: dict[str, object]) -> bool:
        if not self.allow_fixed_ps or not isinstance(args, (list, tuple)) or not args:
            return False
        executable = args[0]
        if executable not in {"/bin/ps", "/usr/bin/ps"} or not Path(str(executable)).is_file():
            return False
        expected = [str(executable), "-o", "rss=", "-p", str(os.getpid())]
        if [str(value) for value in args] != expected:
            return False
        return all(
            name in self._SAFE_POPEN_OPTIONS and value == self._SAFE_POPEN_OPTIONS[name]
            for name, value in kwargs.items()
        )

    def _native_dispatch_allowed(self, path: object, argv: object) -> bool:
        return bool(
            getattr(self._thread_state, "fixed_ps_authorized", False)
            and self.allow_fixed_ps
            and isinstance(path, str)
            and path in {"/bin/ps", "/usr/bin/ps"}
            and Path(path).is_file()
            and isinstance(argv, (list, tuple))
            and [str(value) for value in argv] == [path, "-o", "rss=", "-p", str(os.getpid())]
        )

    def _patch(self, target: object, name: str, replacement: object) -> None:
        original = getattr(target, name)
        self._patched.append((target, name, original))
        setattr(target, name, replacement)
        self.installed_hooks.append(f"{getattr(target, '__name__', type(target).__name__)}.{name}")

    def install(self) -> None:
        if self._patched:
            raise RuntimeError("process guard is already installed")
        original_popen_init: Callable[..., object] = subprocess.Popen.__init__
        execute_child = getattr(subprocess.Popen, "_execute_child", None)
        if not callable(execute_child):
            raise RuntimeError("subprocess.Popen._execute_child is unavailable")
        original_execute_child: Callable[..., object] = execute_child

        def guarded_popen_init(
            popen: subprocess.Popen[Any], args: object, *popen_args: object, **kwargs: object
        ) -> None:
            if popen_args or not self._is_fixed_ps(args, kwargs):
                self._deny("subprocess.Popen")
            self.fixed_ps_attempts += 1
            previous = getattr(self._thread_state, "fixed_ps_authorized", False)
            self._thread_state.fixed_ps_authorized = True
            try:
                original_popen_init(popen, args, **kwargs)
                return None
            finally:
                self._thread_state.fixed_ps_authorized = previous

        def guarded_execute_child(
            popen: subprocess.Popen[Any], *args: object, **kwargs: object
        ) -> object:
            if not getattr(self._thread_state, "fixed_ps_authorized", False):
                self._deny("subprocess.Popen._execute_child")
            return original_execute_child(popen, *args, **kwargs)

        try:
            self._patch(subprocess.Popen, "__init__", guarded_popen_init)
            self._patch(subprocess.Popen, "_execute_child", guarded_execute_child)
            for name in sorted(dir(os)):
                if name.startswith(("exec", "spawn")) or name in self._OS_PROCESS_METHODS:
                    original = getattr(os, name, None)
                    if not callable(original):
                        continue
                    if name in {"posix_spawn", "posix_spawnp"}:

                        def guarded_spawn(
                            path: object,
                            argv: object,
                            *spawn_args: object,
                            _original: Callable[..., object] = original,
                            _name: str = name,
                            **spawn_kwargs: object,
                        ) -> object:
                            if spawn_args or not self._native_dispatch_allowed(path, argv):
                                self._deny(f"os.{_name}")
                            return _original(path, argv, *spawn_args, **spawn_kwargs)

                        replacement: object = guarded_spawn
                    else:

                        def guarded_os_process(
                            *_args: object, _name: str = name, **_kwargs: object
                        ) -> object:
                            self._deny(f"os.{_name}")

                        replacement = guarded_os_process
                    self._patch(os, name, replacement)

            with suppress(ImportError):
                posix_subprocess = importlib.import_module("_posixsubprocess")
                original_fork_exec = getattr(posix_subprocess, "fork_exec", None)
                if callable(original_fork_exec):

                    def guarded_fork_exec(*args: object, **kwargs: object) -> object:
                        if not getattr(self._thread_state, "fixed_ps_authorized", False):
                            self._deny("_posixsubprocess.fork_exec")
                        return original_fork_exec(*args, **kwargs)

                    self._patch(posix_subprocess, "fork_exec", guarded_fork_exec)
        except Exception:
            self.restore()
            raise

    def restore(self) -> None:
        for target, name, original in reversed(self._patched):
            setattr(target, name, original)
        self._patched.clear()

    def assert_no_blocked_attempts(self) -> None:
        if self.blocked_attempts:
            raise RuntimeError(
                "worker observed blocked process attempts: "
                f"{json.dumps(self.blocked_reasons, sort_keys=True)}"
            )

    def evidence(self) -> dict[str, object]:
        return {
            "mode": "worker_process_guard",
            "fixed_ps_attempts": self.fixed_ps_attempts,
            "blocked_attempts": self.blocked_attempts,
            "blocked_reasons": dict(sorted(self.blocked_reasons.items())),
            "installed_hooks": sorted(self.installed_hooks),
            "native_syscall_boundary": (
                "Python entry points only; arbitrary native syscalls are not proven blocked"
            ),
        }


class _TaskHTTPGuard:
    """Deny dispatch through installed Python HTTP clients before service imports."""

    _PACKAGE_TARGETS = {
        "requests": (
            ("requests.sessions", "Session", "request"),
            ("requests.sessions", "Session", "send"),
        ),
        "urllib3": (
            ("urllib3.connectionpool", "HTTPConnectionPool", "urlopen"),
            ("urllib3.connectionpool", "HTTPSConnectionPool", "urlopen"),
            ("urllib3.poolmanager", "PoolManager", "urlopen"),
        ),
        "httpx": (
            ("httpx", "Client", "send"),
            ("httpx", "AsyncClient", "send"),
        ),
        "aiohttp": (("aiohttp", "ClientSession", "_request"),),
        "curl_cffi": (
            ("curl_cffi", "Curl", "perform"),
            ("curl_cffi.requests.session", "Session", "request"),
        ),
    }

    def __init__(self) -> None:
        self.blocked_attempts = 0
        self.blocked_reasons: dict[str, int] = {}
        self.installed_hooks: list[str] = []
        self._patched: list[tuple[object, str, object]] = []

    def _deny(self, name: str) -> Callable[..., NoReturn]:
        def denied(*_args: object, **_kwargs: object) -> NoReturn:
            self.blocked_attempts += 1
            self.blocked_reasons[name] = self.blocked_reasons.get(name, 0) + 1
            raise PermissionError(f"isolated worker HTTP guard blocked {name}")

        return denied

    def _patch(self, target: object, name: str, path: str) -> None:
        original = getattr(target, name, None)
        if not callable(original):
            raise RuntimeError(f"installed HTTP dispatch point is unavailable: {path}")
        self._patched.append((target, name, original))
        setattr(target, name, self._deny(path))
        self.installed_hooks.append(path)

    def install(self) -> None:
        if self._patched:
            raise RuntimeError("HTTP guard is already installed")
        try:
            urllib_request = importlib.import_module("urllib.request")
            http_client = importlib.import_module("http.client")
            self._patch(urllib_request, "urlopen", "urllib.request.urlopen")
            self._patch(
                http_client.HTTPConnection,
                "connect",
                "http.client.HTTPConnection.connect",
            )
            self._patch(
                http_client.HTTPSConnection,
                "connect",
                "http.client.HTTPSConnection.connect",
            )
            for package, targets in self._PACKAGE_TARGETS.items():
                if importlib.util.find_spec(package) is None:
                    continue
                for module_name, class_name, attribute in targets:
                    module = importlib.import_module(module_name)
                    target = getattr(module, class_name, None)
                    if target is None:
                        raise RuntimeError(
                            "installed HTTP dispatch class is unavailable: "
                            f"{module_name}.{class_name}"
                        )
                    self._patch(
                        target,
                        attribute,
                        f"{module_name}.{class_name}.{attribute}",
                    )
            if importlib.util.find_spec("pycurl") is not None:
                pycurl = importlib.import_module("pycurl")
                original_curl = getattr(pycurl, "Curl", None)
                if not callable(original_curl):
                    raise RuntimeError("installed HTTP dispatch class is unavailable: pycurl.Curl")
                self._patch(pycurl, "Curl", "pycurl.Curl")
        except Exception:
            self.restore()
            raise

    def restore(self) -> None:
        for target, name, original in reversed(self._patched):
            setattr(target, name, original)
        self._patched.clear()

    def assert_no_blocked_attempts(self) -> None:
        if self.blocked_attempts:
            raise RuntimeError(
                "worker observed blocked HTTP attempts: "
                f"{json.dumps(self.blocked_reasons, sort_keys=True)}"
            )

    def evidence(self) -> dict[str, object]:
        return {
            "mode": "deny_all_python_http_dispatch",
            "installed_hooks": sorted(self.installed_hooks),
            "blocked_attempts": self.blocked_attempts,
            "blocked_reasons": dict(sorted(self.blocked_reasons.items())),
        }


_SQL_OBJECT_RE = re.compile(
    r"\b(?:FROM|JOIN|UPDATE|INTO|TABLE|REFERENCES)\s+"
    r"(?:IF\s+(?:NOT\s+)?EXISTS\s+)?"
    r"(?:`([^`]+)`|\"([^\"]+)\"|([A-Za-z_][\w$]*))"
    r"\s*\.\s*(?:`([^`]+)`|\"([^\"]+)\"|([A-Za-z_][\w$]*))",
    re.IGNORECASE,
)


class _TaskSQLGuard:
    """Constrain SQLAlchemy statements to the worker's exact schemas/roles."""

    _READ_ONLY = {"SELECT", "SHOW", "EXPLAIN", "DESC", "DESCRIBE", "SET"}
    _TARGET_ALLOWED = _READ_ONLY | {
        "INSERT",
        "UPDATE",
        "DELETE",
        "TRUNCATE",
        "REPLACE",
        "CREATE",
        "DROP",
        "ALTER",
    }

    def __init__(self, source_schema: str, target_schema: str) -> None:
        if not _SAFE_SCHEMA.fullmatch(source_schema) or not _SAFE_SCHEMA.fullmatch(target_schema):
            raise ValueError("SQL guard received a non-task schema")
        if not source_schema.endswith("_source") or not target_schema.endswith("_target"):
            raise ValueError("SQL guard schema roles are invalid")
        if source_schema.rsplit("_", 1)[0] != target_schema.rsplit("_", 1)[0]:
            raise ValueError("SQL guard schemas are not a matched pair")
        self.schemas = {"source": source_schema, "target": target_schema}

    @staticmethod
    def _first_keyword(statement: str) -> str:
        value = statement.lstrip()
        while value.startswith(("--", "/*")):
            if value.startswith("--"):
                newline = value.find("\n")
                if newline < 0:
                    return ""
                value = value[newline + 1 :].lstrip()
            else:
                end = value.find("*/", 2)
                if end < 0:
                    return ""
                value = value[end + 2 :].lstrip()
        keyword = re.match(r"([A-Za-z]+)", value)
        return keyword.group(1).upper() if keyword else ""

    @staticmethod
    def _qualified_schemas(statement: str) -> list[str]:
        return [
            next(part for part in match.groups()[:3] if part is not None)
            for match in _SQL_OBJECT_RE.finditer(statement)
        ]

    def before_cursor_execute(
        self,
        connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        engine = getattr(connection, "engine", None)
        database = getattr(getattr(engine, "url", None), "database", None)
        if database not in self.schemas.values():
            raise PermissionError("SQL guard blocked an unowned schema connection")
        role = "source" if database == self.schemas["source"] else "target"
        first = self._first_keyword(statement)
        if first in {"USE", "GRANT", "REVOKE"} or re.match(
            r"\s*(?:CREATE|ALTER|DROP)\s+(?:DATABASE|SCHEMA|USER|ROLE|TABLESPACE)\b",
            statement,
            re.IGNORECASE,
        ):
            raise PermissionError("SQL guard blocked a database-level or privilege statement")
        if role == "source" and first not in self._READ_ONLY:
            raise PermissionError("SQL guard blocked a source-side write")
        if role == "target" and first not in self._TARGET_ALLOWED:
            raise PermissionError("SQL guard blocked an unsupported target statement")
        if first == "SET" and re.search(
            r"\b(?:GLOBAL|PERSIST|PERSIST_ONLY)\b", statement, re.IGNORECASE
        ):
            raise PermissionError("SQL guard blocked a global server setting")
        for referenced_schema in self._qualified_schemas(statement):
            if referenced_schema != database:
                raise PermissionError("SQL guard blocked a cross-schema reference")


def _require_owned_resource(labels_json: str, run_id: str, *, purpose: str) -> dict[str, str]:
    labels = json.loads(labels_json)
    if not isinstance(labels, dict):
        raise RuntimeError("Docker ownership labels are malformed")
    if labels.get(RUN_LABEL) != run_id or labels.get(PURPOSE_LABEL) != purpose:
        raise RuntimeError("Docker ownership check failed; refusing to mutate resource")
    return {str(key): str(value) for key, value in labels.items()}


def _port_mapping_evidence(
    container_result: dict[str, Any], *, expected_port: int, published_output: str
) -> dict[str, Any]:
    """Capture and validate the exact task-owned loopback mapping without env secrets."""
    config = container_result.get("Config")
    host_config = container_result.get("HostConfig")
    network_settings = container_result.get("NetworkSettings")
    bindings = host_config.get("PortBindings") if isinstance(host_config, dict) else None
    mysql_bindings = bindings.get("3306/tcp") if isinstance(bindings, dict) else None
    ports = network_settings.get("Ports") if isinstance(network_settings, dict) else None
    mysql_ports = ports.get("3306/tcp") if isinstance(ports, dict) else None
    published_lines = published_output.splitlines()
    published_port: int | None = None
    if len(published_lines) == 1:
        try:
            published_port = _parse_loopback_port(published_lines[0])
        except ValueError:
            published_port = None

    configured_binding = (
        mysql_bindings[0]
        if isinstance(mysql_bindings, list)
        and len(mysql_bindings) == 1
        and isinstance(mysql_bindings[0], dict)
        else None
    )
    network_binding = (
        mysql_ports[0]
        if isinstance(mysql_ports, list)
        and len(mysql_ports) == 1
        and isinstance(mysql_ports[0], dict)
        else None
    )
    checks = {
        "only_mysql_tcp_binding_configured": isinstance(bindings, dict)
        and set(bindings) == {"3306/tcp"}
        and isinstance(mysql_bindings, list)
        and len(mysql_bindings) == 1,
        "configured_host_ip_is_loopback": isinstance(configured_binding, dict)
        and configured_binding.get("HostIp") == "127.0.0.1",
        "configured_host_port_requests_dynamic_assignment": isinstance(configured_binding, dict)
        and configured_binding.get("HostPort") == "",
        "network_settings_host_ip_is_loopback": isinstance(ports, dict)
        and set(ports) == {"3306/tcp"}
        and isinstance(mysql_ports, list)
        and len(mysql_ports) == 1
        and isinstance(network_binding, dict)
        and network_binding.get("HostIp") == "127.0.0.1",
        "network_settings_host_port_matches": isinstance(network_binding, dict)
        and network_binding.get("HostPort") == str(expected_port),
        "docker_port_output_is_exact_loopback_endpoint": len(published_lines) == 1
        and published_port == expected_port,
    }
    labels = config.get("Labels") if isinstance(config, dict) else None
    sanitized_inspect = {
        "Id": container_result.get("Id"),
        "Name": container_result.get("Name"),
        "Config": {
            "Image": config.get("Image") if isinstance(config, dict) else None,
            "Labels": {
                RUN_LABEL: labels.get(RUN_LABEL) if isinstance(labels, dict) else None,
                PURPOSE_LABEL: labels.get(PURPOSE_LABEL) if isinstance(labels, dict) else None,
            },
        },
        "HostConfig": {
            "PortBindings": bindings,
            "NetworkMode": (
                host_config.get("NetworkMode") if isinstance(host_config, dict) else None
            ),
            "Memory": host_config.get("Memory") if isinstance(host_config, dict) else None,
            "NanoCpus": host_config.get("NanoCpus") if isinstance(host_config, dict) else None,
        },
        "NetworkSettings": {"Ports": ports},
    }
    return {
        "schema_version": 1,
        "evidence_type": "task_container_port_mapping_preflight",
        "observed_at_utc": datetime.now(timezone.utc).isoformat(),
        "expected": {
            "container_id": container_result.get("Id"),
            "container_name": container_result.get("Name"),
            "config_binding_keys": ["3306/tcp"],
            "host_ip": "127.0.0.1",
            "host_config_host_port": "",
            "assigned_host_port": str(expected_port),
            "published_endpoint": f"127.0.0.1:{expected_port}",
        },
        "actual": {
            "sanitized_container_inspect": sanitized_inspect,
            "docker_port_output": published_output,
            "docker_port_lines": published_lines,
        },
        "checks": checks,
        "valid": all(checks.values()),
    }


def _validate_worker_schema_pair(
    label: str,
    run_suffix: str,
    source_schema: str,
    target_schema: str,
) -> None:
    if label not in {"before", "after"} or not re.fullmatch(r"[a-f0-9]{12}", run_suffix):
        raise ValueError("worker snapshot identity is invalid")
    if source_schema != _schema_name(run_suffix, f"{label}_source"):
        raise ValueError("worker source schema is not owned by this snapshot")
    if target_schema != _schema_name(run_suffix, f"{label}_target"):
        raise ValueError("worker target schema is not owned by this snapshot")


def _verify_worker_container(
    args: argparse.Namespace, *, diagnostic_path: Path | None = None
) -> dict[str, Any]:
    """Fail closed unless CLI proof matches this task's live Docker resources."""
    docker_endpoint = _require_local_docker_endpoint()
    if args.docker_endpoint != docker_endpoint:
        raise RuntimeError("worker Docker endpoint changed from the controller's local endpoint")
    run_id = args.run_id
    container_id = args.container_id
    container_name = args.container_name
    volume_name = args.volume_name
    if not all(
        isinstance(value, str) and value
        for value in (run_id, container_id, container_name, volume_name)
    ):
        raise RuntimeError("worker is missing task container ownership proof")
    try:
        if str(uuid.UUID(run_id)) != run_id:
            raise ValueError("non-canonical run id")
    except ValueError as exc:
        raise RuntimeError("worker run ID is not a canonical UUID") from exc
    name_match = re.fullmatch(r"opendata-c100-perf-\d{8}T\d{6}Z-([a-f0-9]{12})", container_name)
    if name_match is None or volume_name != f"opendata-c100-perf-{name_match.group(1)}":
        raise RuntimeError("worker container/volume names are outside the task naming scheme")
    if not re.fullmatch(r"[a-f0-9]{64}", container_id):
        raise RuntimeError("worker container ID is not a full immutable Docker ID")
    if not 1 <= args.port <= 65535 or args.port in {3306, 33565}:
        raise RuntimeError("worker port is invalid or protected")
    _validate_worker_schema_pair(
        args.label, name_match.group(1), args.source_schema, args.target_schema
    )

    container_result = json.loads(_docker(["inspect", "--format", "{{json .}}", container_id]))
    if not isinstance(container_result, dict):
        raise RuntimeError("worker container inspection was malformed")
    if container_result.get("Id") != container_id:
        raise RuntimeError("worker container ID did not match the inspected resource")
    if container_result.get("Name") != f"/{container_name}":
        raise RuntimeError("worker container name did not match the inspected resource")
    config = container_result.get("Config")
    if not isinstance(config, dict) or config.get("Image") != MYSQL_IMAGE:
        raise RuntimeError("worker container does not use the exact pinned MySQL image")
    labels = _require_owned_resource(
        json.dumps(config.get("Labels", {})), run_id, purpose=PROBE_PURPOSE
    )
    state = container_result.get("State")
    if not isinstance(state, dict) or state.get("Running") is not True:
        raise RuntimeError("worker task MySQL container is not running")

    image_id = container_result.get("Image")
    if not isinstance(image_id, str) or not image_id:
        raise RuntimeError("worker container does not identify its immutable image")
    image_result = json.loads(_docker(["image", "inspect", "--format", "{{json .}}", image_id]))
    expected_digest = MYSQL_IMAGE.rsplit("@", 1)[1]
    repo_digests = image_result.get("RepoDigests") if isinstance(image_result, dict) else None
    if not isinstance(repo_digests, list) or not any(
        isinstance(value, str) and value.endswith(f"@{expected_digest}") for value in repo_digests
    ):
        raise RuntimeError("worker image digest does not match the pinned platform manifest")

    host_config = container_result.get("HostConfig")
    if not isinstance(host_config, dict):
        raise RuntimeError("worker container has malformed host configuration")
    published_output = _docker(["port", container_id, "3306/tcp"])
    port_evidence = _port_mapping_evidence(
        container_result, expected_port=args.port, published_output=published_output
    )
    port_evidence["observed_at_utc"] = datetime.now(timezone.utc).isoformat()
    port_evidence["run_id"] = run_id
    if diagnostic_path is not None:
        _write_json(diagnostic_path, port_evidence)
    if port_evidence["valid"] is not True:
        raise RuntimeError(
            "worker Docker port mapping is not the exact loopback endpoint; "
            "see the redacted container port preflight evidence"
        )
    if host_config.get("NetworkMode") not in {"default", "bridge"}:
        raise RuntimeError("worker container network mode is not the task default bridge")

    mounts = container_result.get("Mounts")
    if (
        not isinstance(mounts, list)
        or len(mounts) != 1
        or not isinstance(mounts[0], dict)
        or mounts[0].get("Type") != "volume"
        or mounts[0].get("Name") != volume_name
        or mounts[0].get("Destination") != "/var/lib/mysql"
        or mounts[0].get("RW") is not True
    ):
        raise RuntimeError("worker container volume mount is not the exact task-owned volume")
    volume_result = json.loads(_docker(["volume", "inspect", volume_name]))
    if not isinstance(volume_result, list) or len(volume_result) != 1:
        raise RuntimeError("worker task volume inspection was malformed")
    volume = volume_result[0]
    if (
        not isinstance(volume, dict)
        or volume.get("Name") != volume_name
        or volume.get("Driver") != "local"
        or volume.get("Scope") != "local"
    ):
        raise RuntimeError("worker volume is not the task-owned local volume")
    volume_labels = _require_owned_resource(
        json.dumps(volume.get("Labels", {})), run_id, purpose=PROBE_PURPOSE
    )
    task_schemas = sorted(
        _schema_name(name_match.group(1), f"{side}_{role}")
        for side in ("before", "after")
        for role in ("source", "target")
    )
    password = getattr(args, "task_password", None) or os.environ.get(_PASSWORD_ENV)
    if not password:
        raise RuntimeError("worker requires the ephemeral task-generated MySQL password")
    schema_query = (
        "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA "
        "WHERE SCHEMA_NAME REGEXP "
        "'^opendata_c100_[a-f0-9]{12}_(before|after)_(source|target)$' "
        "ORDER BY SCHEMA_NAME"
    )
    docker_env = os.environ.copy()
    docker_env["MYSQL_PWD"] = password
    schema_result = _run_command(
        [
            _executable("docker"),
            "exec",
            "-e",
            "MYSQL_PWD",
            container_id,
            "mysql",
            "--host=127.0.0.1",
            "--user=root",
            "--batch",
            "--skip-column-names",
            f"--execute={schema_query}",
        ],
        env=docker_env,
    )
    observed_schemas = schema_result.stdout.splitlines()
    if schema_result.returncode != 0 or observed_schemas != task_schemas:
        raise RuntimeError("worker task schema inventory did not match the four owned schemas")

    source_root = Path(args.source_root).resolve()
    runner_path = source_root / "scripts/quality/iter2_performance_probe.py"
    if (
        not (source_root / "opendata").is_dir()
        or not runner_path.is_file()
        or not (source_root / "pyproject.toml").is_file()
        or (source_root / ".env").exists()
        or source_root.is_symlink()
        or runner_path.is_symlink()
    ):
        raise RuntimeError(
            "worker source bundle is missing required files or contains a local .env"
        )
    return {
        "run_id": run_id,
        "container_id": container_id,
        "container_name": container_name,
        "volume_name": volume_name,
        "port": args.port,
        "docker_endpoint": docker_endpoint,
        "image": MYSQL_IMAGE,
        "container_labels": labels,
        "volume_labels": volume_labels,
        "schemas": {"source": args.source_schema, "target": args.target_schema},
        "verified_task_schemas": task_schemas,
        "snapshot_source_root": str(source_root),
    }


def _controller_worker_proof(
    *,
    label: str,
    source_root: Path,
    source_schema: str,
    target_schema: str,
    port: int,
    run_id: str,
    container_id: str,
    container_name: str,
    volume_name: str,
    docker_endpoint: str,
    source_bundle: dict[str, object],
    bundle_directory: Path,
    task_password: str,
) -> dict[str, Any]:
    """Verify Docker ownership in the controller and bind proof to frozen inputs."""
    args = argparse.Namespace(
        label=label,
        source_root=str(source_root),
        source_schema=source_schema,
        target_schema=target_schema,
        port=port,
        run_id=run_id,
        container_id=container_id,
        container_name=container_name,
        volume_name=volume_name,
        docker_endpoint=docker_endpoint,
        task_password=task_password,
    )
    proof = _verify_worker_container(
        args, diagnostic_path=bundle_directory / f"{label}-container-port-preflight.json"
    )
    root = Path(proof["snapshot_source_root"])
    archive_path = bundle_directory / str(source_bundle.get("archive_file"))
    manifest_path = bundle_directory / str(source_bundle.get("manifest_file"))
    if (
        not archive_path.is_file()
        or not manifest_path.is_file()
        or _sha256_file(archive_path) != source_bundle.get("archive_sha256")
        or _sha256_file(manifest_path) != source_bundle.get("manifest_sha256")
    ):
        raise RuntimeError("controller source bundle evidence changed after extraction")
    bundle_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        not isinstance(bundle_manifest, dict)
        or bundle_manifest.get("role") != label
        or bundle_manifest.get("archive_sha256") != source_bundle.get("archive_sha256")
        or bundle_manifest.get("source_package_tree_sha256")
        != source_bundle.get("source_package_tree_sha256")
    ):
        raise RuntimeError("controller source bundle manifest does not match worker role")
    bundle_entries = bundle_manifest.get("files")
    if not isinstance(bundle_entries, list):
        raise RuntimeError("controller source bundle inventory is malformed")
    expected_paths = {str(entry.get("path")) for entry in bundle_entries if isinstance(entry, dict)}
    actual_paths = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    if expected_paths != actual_paths:
        raise RuntimeError("extracted worker sources differ from the retained bundle inventory")
    for entry in bundle_entries:
        if not isinstance(entry, dict):
            raise RuntimeError("controller source bundle inventory entry is malformed")
        source_path = root / str(entry["path"])
        if source_path.stat().st_size != entry.get("size_bytes") or _sha256_file(
            source_path
        ) != entry.get("sha256"):
            raise RuntimeError("extracted worker source differs from its retained bundle hash")
    proof["source_bundle_role"] = label
    proof["source_package_archive_tree_sha256"] = str(source_bundle["source_package_tree_sha256"])
    proof["extracted_package_tree_sha256"] = _tree_sha256(root / "opendata")
    proof["source_bundle_archive_sha256"] = str(source_bundle["archive_sha256"])
    proof["source_bundle_manifest_sha256"] = str(source_bundle["manifest_sha256"])
    proof["runner_sha256"] = _sha256_file(root / "scripts/quality/iter2_performance_probe.py")
    proof["pyproject_sha256"] = _sha256_file(root / "pyproject.toml")
    return proof


def _worker_frozen_input_claim(proof: dict[str, Any]) -> dict[str, str]:
    """Select exact archive fingerprints that the worker must echo in its receipt."""
    return {
        "archive_root": str(proof["snapshot_source_root"]),
        "source_package_archive_tree_sha256": str(proof["source_package_archive_tree_sha256"]),
        "extracted_package_tree_sha256": str(proof["extracted_package_tree_sha256"]),
        "source_bundle_archive_sha256": str(proof["source_bundle_archive_sha256"]),
        "source_bundle_manifest_sha256": str(proof["source_bundle_manifest_sha256"]),
        "runner_sha256": str(proof["runner_sha256"]),
        "pyproject_sha256": str(proof["pyproject_sha256"]),
    }


def _validate_preflight_worker_input(worker_result: dict[str, Any], proof: dict[str, Any]) -> None:
    """Reject a preflight result unless it identifies the exact sealed worker input."""
    expected = _worker_frozen_input_claim(proof)
    if (
        worker_result.get("label") != proof.get("source_bundle_role")
        or worker_result.get("status") != "preflight_pass"
        or worker_result.get("source_tree_sha256") != expected["extracted_package_tree_sha256"]
        or worker_result.get("runner_sha256") != expected["runner_sha256"]
        or worker_result.get("frozen_input") != expected
    ):
        raise RuntimeError("worker preflight result does not match its sealed source bundle")


def _validate_worker_proof(args: argparse.Namespace) -> dict[str, Any]:
    """Validate controller-owned container proof and the immutable archive-local runner."""
    try:
        proof = json.loads(args.worker_proof_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("worker is missing the controller's verified Docker proof") from exc
    if not isinstance(proof, dict):
        raise RuntimeError("worker controller proof is not a JSON object")
    root = Path(args.source_root).resolve()
    runner_path = root / "scripts/quality/iter2_performance_probe.py"
    if (
        proof.get("snapshot_source_root") != str(root)
        or Path(__file__).resolve() != runner_path.resolve()
        or Path.cwd().resolve() != root
        or os.environ.get("PYTHONPATH") != str(root)
        or not (root / "opendata").is_dir()
        or not runner_path.is_file()
        or (root / ".env").exists()
    ):
        raise RuntimeError("worker process is not executing from its retained source bundle")
    expected_fields = {
        "port": args.port,
        "run_id": args.run_id,
        "container_id": args.container_id,
        "container_name": args.container_name,
        "volume_name": args.volume_name,
        "docker_endpoint": args.docker_endpoint,
        "image": MYSQL_IMAGE,
        "extracted_package_tree_sha256": _tree_sha256(root / "opendata"),
        "runner_sha256": _sha256_file(runner_path),
        "pyproject_sha256": _sha256_file(root / "pyproject.toml"),
    }
    for name, value in expected_fields.items():
        if proof.get(name, object()) != value:
            raise RuntimeError(f"worker controller proof does not match {name}")
    for name in (
        "source_package_archive_tree_sha256",
        "source_bundle_archive_sha256",
        "source_bundle_manifest_sha256",
    ):
        if not isinstance(proof.get(name), str) or not re.fullmatch(r"[a-f0-9]{64}", proof[name]):
            raise RuntimeError(f"worker controller proof has invalid {name}")
    if proof.get("source_bundle_role") != args.label:
        raise RuntimeError("worker controller proof does not match its source bundle role")
    suffix_match = re.fullmatch(
        r"opendata-c100-perf-\d{8}T\d{6}Z-([a-f0-9]{12})", str(args.container_name)
    )
    if suffix_match is None:
        raise RuntimeError("worker container identity is outside the task naming scheme")
    suffix = suffix_match.group(1)
    _validate_worker_schema_pair(args.label, suffix, args.source_schema, args.target_schema)
    expected_schemas = sorted(
        _schema_name(suffix, f"{side}_{role}")
        for side in ("before", "after")
        for role in ("source", "target")
    )
    container_labels = proof.get("container_labels")
    volume_labels = proof.get("volume_labels")
    if (
        proof.get("verified_task_schemas") != expected_schemas
        or proof.get("schemas") != {"source": args.source_schema, "target": args.target_schema}
        or not isinstance(container_labels, dict)
        or container_labels.get(RUN_LABEL) != args.run_id
        or container_labels.get(PURPOSE_LABEL) != PROBE_PURPOSE
        or not isinstance(volume_labels, dict)
        or volume_labels.get(RUN_LABEL) != args.run_id
        or volume_labels.get(PURPOSE_LABEL) != PROBE_PURPOSE
    ):
        raise RuntimeError("worker controller proof has invalid labels or schema ownership")
    if (
        not isinstance(args.port, int)
        or args.port in {3306, 33565}
        or not isinstance(proof.get("docker_endpoint"), str)
        or not str(proof["docker_endpoint"]).startswith("unix://")
        or not isinstance(proof.get("container_id"), str)
        or not re.fullmatch(r"[a-f0-9]{64}", str(proof["container_id"]))
        or not isinstance(proof.get("volume_name"), str)
        or proof.get("volume_name") != f"opendata-c100-perf-{suffix}"
    ):
        raise RuntimeError("worker port is invalid or protected")
    bundle_entries = [root, *root.rglob("*")]
    if any(path.stat().st_mode & 0o222 for path in bundle_entries):
        raise RuntimeError("worker source bundle is writable and cannot be treated as immutable")
    return proof


def _image_available() -> None:
    inspected = _docker(["image", "inspect", MYSQL_IMAGE], check=False)
    if inspected:
        return
    _docker(["pull", "--platform", MYSQL_IMAGE_PLATFORM, MYSQL_IMAGE])


def _wait_mysql(container_id: str, password: str, timeout_seconds: int = 150) -> None:
    deadline = time.monotonic() + timeout_seconds
    env = os.environ.copy()
    env["MYSQL_PWD"] = password
    while time.monotonic() < deadline:
        result = _run_command(
            [
                "docker",
                "exec",
                "-e",
                "MYSQL_PWD",
                container_id,
                "mysqladmin",
                "ping",
                "-h127.0.0.1",
                "-uroot",
            ],
            env=env,
        )
        if result.returncode == 0 and "mysqld is alive" in result.stdout:
            return
        time.sleep(2)
    raise TimeoutError("task-owned MySQL did not become ready within the bounded startup window")


def _write_secret_env_file(path: Path, password: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(f"MYSQL_ROOT_PASSWORD={password}\nMYSQL_ROOT_HOST=%\n")


def _child_env(
    password: str,
    *,
    port: int | None = None,
    database: str | None = None,
    pythonpath: Path | None = None,
) -> dict[str, str]:
    env = os.environ.copy()
    for name in ("PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE", "PYTHONSTARTUP"):
        env.pop(name, None)
    for name in list(env):
        upper = name.upper()
        if upper.endswith("_DATABASE_URL") or any(
            marker in upper
            for marker in (
                "DATABASE_URL",
                "_DB_",
                "DB_",
                "DB_URL",
                "MYSQL",
                "POSTGRES",
                "API_KEY",
                "TOKEN",
                "PASSWORD",
                "SECRET",
            )
        ):
            env.pop(name, None)
    env.update(
        {
            _PASSWORD_ENV: password,
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHON_DOTENV_DISABLED": "1",
            "TESTING": "true",
            "APP_ENV": "testing",
        }
    )
    if pythonpath is not None:
        if not pythonpath.is_absolute() or not (pythonpath / "opendata").is_dir():
            raise ValueError("child PYTHONPATH must be an absolute frozen source bundle root")
        env["PYTHONPATH"] = str(pythonpath.resolve())
    if port is not None or database is not None:
        if port is None or database is None:
            raise ValueError("isolated worker SQL configuration needs both port and schema")
        if not 1 <= port <= 65535 or port in {3306, 33565}:
            raise ValueError("isolated worker SQL configuration uses an invalid port")
        if not _SAFE_SCHEMA.fullmatch(database):
            raise ValueError("isolated worker SQL configuration uses an unowned schema")
        env.update(
            {
                "MYSQL_HOST": "127.0.0.1",
                "MYSQL_PORT": str(port),
                "MYSQL_USER": "root",
                "MYSQL_PASSWORD": password,
                "MYSQL_DATABASE": database,
                "DATA_MYSQL_HOST": "127.0.0.1",
                "DATA_MYSQL_PORT": str(port),
                "DATA_MYSQL_USER": "root",
                "DATA_MYSQL_PASSWORD": password,
                "DATA_MYSQL_DATABASE": database,
            }
        )
    return env


def _create_mysql_container(run_id: str, run_suffix: str, run_dir: Path) -> dict[str, Any]:
    container_name = f"opendata-c100-perf-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{run_suffix}"
    volume_name = f"opendata-c100-perf-{run_suffix}"
    container_exists = _docker_resource_exists("container", container_name)
    volume_exists = _docker_resource_exists("volume", volume_name)
    if container_exists or volume_exists:
        raise RuntimeError("unique Docker resource name already exists; refusing to reuse it")

    password = uuid.uuid4().hex + uuid.uuid4().hex
    env_file = run_dir / ".mysql-env"
    _write_secret_env_file(env_file, password)
    labels = [
        "--label",
        f"{RUN_LABEL}={run_id}",
        "--label",
        f"{PURPOSE_LABEL}={PROBE_PURPOSE}",
    ]
    container_id: str | None = None
    try:
        _docker(["volume", "create", *labels, volume_name])
        _require_owned_resource(
            _docker(["volume", "inspect", "--format", "{{json .Labels}}", volume_name]),
            run_id,
            purpose=PROBE_PURPOSE,
        )
        container_id = (
            _docker(
                [
                    "run",
                    "--detach",
                    "--name",
                    container_name,
                    *labels,
                    "--env-file",
                    str(env_file),
                    "--publish",
                    "127.0.0.1::3306",
                    "--mount",
                    f"type=volume,src={volume_name},dst=/var/lib/mysql",
                    "--restart",
                    "no",
                    "--memory",
                    "2g",
                    "--cpus",
                    "2.0",
                    MYSQL_IMAGE,
                ]
            )
            .splitlines()[0]
            .strip()
        )
        container_labels = _require_owned_resource(
            _docker(["inspect", "--format", "{{json .Config.Labels}}", container_id]),
            run_id,
            purpose=PROBE_PURPOSE,
        )
        inspect = json.loads(_docker(["inspect", "--format", "{{json .}}", container_id]))
        if inspect.get("Id") != container_id:
            raise RuntimeError("Docker container ID changed after creation")
        if inspect.get("Name") != f"/{container_name}":
            raise RuntimeError("Docker container name changed after creation")
        if not any(
            item.get("Name") == volume_name
            for item in inspect.get("Mounts", [])
            if isinstance(item, dict)
        ):
            raise RuntimeError("Docker container is not mounted to the task-owned volume")
        published = _docker(["port", container_id, "3306/tcp"]).splitlines()
        if len(published) != 1:
            raise RuntimeError("container must publish exactly one IPv4 loopback endpoint")
        port = _parse_loopback_port(published[0])
        _wait_mysql(container_id, password)
        return {
            "container_name": container_name,
            "container_id": container_id,
            "container_labels": container_labels,
            "volume_name": volume_name,
            "published_endpoint": published[0],
            "host_port": port,
            "image": MYSQL_IMAGE,
            "image_platform": MYSQL_IMAGE_PLATFORM,
            "password": password,
        }
    except Exception:
        env_file.unlink(missing_ok=True)
        if container_id:
            inspected_id = _docker(["inspect", "--format", "{{.Id}}", container_id], check=False)
            if inspected_id:
                labels_json = _docker(
                    ["inspect", "--format", "{{json .Config.Labels}}", container_id]
                )
                _require_owned_resource(labels_json, run_id, purpose=PROBE_PURPOSE)
                _docker(["rm", "--force", container_id])
        else:
            partially_created = _docker(
                ["inspect", "--format", "{{json .}}", container_name], check=False
            )
            if partially_created:
                partial = json.loads(partially_created)
                partial_id = partial.get("Id")
                if partial.get("Name") != f"/{container_name}" or not isinstance(partial_id, str):
                    raise RuntimeError(
                        "partial container identity is ambiguous; refusing cleanup"
                    ) from None
                _require_owned_resource(
                    json.dumps(partial.get("Config", {}).get("Labels", {})),
                    run_id,
                    purpose=PROBE_PURPOSE,
                )
                _docker(["rm", "--force", partial_id])
        inspected_volume = _docker(
            ["volume", "inspect", "--format", "{{json .Labels}}", volume_name], check=False
        )
        if inspected_volume:
            _require_owned_resource(inspected_volume, run_id, purpose=PROBE_PURPOSE)
            _docker(["volume", "rm", volume_name])
        raise
    finally:
        env_file.unlink(missing_ok=True)


def _cleanup_owned_resources(resources: dict[str, Any], run_id: str) -> dict[str, str]:
    outcomes: dict[str, str] = {}
    container_id = resources.get("container_id")
    container_name = resources.get("container_name")
    volume_name = resources.get("volume_name")
    if container_id and container_name:
        actual_id = _docker(["inspect", "--format", "{{.Id}}", str(container_id)], check=False)
        if actual_id:
            labels_text = _docker(
                ["inspect", "--format", "{{json .Config.Labels}}", str(container_id)]
            )
            _require_owned_resource(labels_text, run_id, purpose=PROBE_PURPOSE)
            actual_name = _docker(["inspect", "--format", "{{.Name}}", str(container_id)])
            if actual_id != container_id or actual_name != f"/{container_name}":
                raise RuntimeError("container identity changed; refusing cleanup")
            _docker(["rm", "--force", str(container_id)])
            outcomes["container"] = "removed_owned_id"
        else:
            outcomes["container"] = "already_absent"
    if volume_name:
        inspected_volume = _docker(
            ["volume", "inspect", "--format", "{{json .}}", str(volume_name)], check=False
        )
        if inspected_volume:
            volume = json.loads(inspected_volume)
            if volume.get("Name") != volume_name:
                raise RuntimeError("volume identity changed; refusing cleanup")
            _require_owned_resource(
                json.dumps(volume.get("Labels", {})), run_id, purpose=PROBE_PURPOSE
            )
            _docker(["volume", "rm", str(volume_name)])
            outcomes["volume"] = "removed_owned_label"
        else:
            outcomes["volume"] = "already_absent"
    return outcomes


def _mysql_url(port: int, schema: str, password: str) -> str:
    from sqlalchemy.engine import URL

    if not _SAFE_SCHEMA.fullmatch(schema):
        raise ValueError("refusing to connect to a schema outside this task's generated namespace")
    return URL.create(
        "mysql+pymysql",
        username="root",
        password=password,
        host="127.0.0.1",
        port=port,
        database=schema,
        query={"charset": "utf8mb4"},
    ).render_as_string(hide_password=False)


def _create_schema(engine: Engine, schema: str, ddl: str) -> None:
    if not _SAFE_SCHEMA.fullmatch(schema):
        raise ValueError("refusing to create a schema outside this task's generated namespace")
    from sqlalchemy import text

    with engine.begin() as connection:
        connection.execute(
            text(f"CREATE DATABASE `{schema}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci")
        )
    schema_engine = _engine_for_schema(engine.url, schema)
    try:
        with schema_engine.begin() as connection:
            connection.execute(text(ddl))
    finally:
        schema_engine.dispose()


def _engine_for_schema(url: URL, schema: str) -> Engine:
    from sqlalchemy import create_engine

    return create_engine(url.set(database=schema), pool_pre_ping=True, pool_size=1, max_overflow=0)


def _fixture_insert(engine: Engine) -> None:
    from sqlalchemy import text

    names = [*SOURCE_COLUMNS, "_source", "_fetched_at", "_batch_id"]
    columns = ", ".join(f"`{name}`" for name in names)
    params = ", ".join(f":{name}" for name in names)
    statement = text(
        f"INSERT INTO `{TABLE}` ({columns}) VALUES ({params})"  # noqa: S608 -- identifiers are constants; values bound # nosec B608
    )
    batch: list[dict[str, object]] = []
    inserted = 0
    with engine.begin() as connection:
        for row in _synthetic_rows():
            record = dict(zip(SOURCE_COLUMNS, row, strict=True))
            record.update(
                {
                    "_source": "ths",
                    "_fetched_at": FIXED_FETCHED_AT,
                    "_batch_id": FIXED_BATCH_ID,
                }
            )
            batch.append(record)
            if len(batch) == PAGE_ROWS:
                connection.execute(statement, batch)
                inserted += len(batch)
                batch.clear()
        if batch:
            connection.execute(statement, batch)
            inserted += len(batch)
    if inserted != EXPECTED_ROWS:
        raise RuntimeError(f"synthetic source insertion was incomplete: {inserted}")


def _persist_ddl_render_process(
    evidence_directory: Path | None,
    *,
    argv: Sequence[str],
    cwd: Path,
    started_at: datetime,
    finished_at: datetime,
    timeout_seconds: int,
    returncode: int | None,
    stdout: str,
    stderr: str,
    status: str,
    error: str | None = None,
) -> None:
    """Persist exact DDL child process evidence without recording its environment."""
    if evidence_directory is None:
        return
    evidence_directory.mkdir(parents=True, exist_ok=True)
    stdout_name = "baseline-ddl-render.stdout.json"
    stderr_name = "baseline-ddl-render.stderr.log"
    record_name = "baseline-ddl-render.process.json"
    record = {
        "argv": list(argv),
        "cwd": str(cwd.resolve()),
        "started_at_utc": started_at.isoformat(),
        "finished_at_utc": finished_at.isoformat(),
        "duration_seconds": round((finished_at - started_at).total_seconds(), 6),
        "timeout_seconds": timeout_seconds,
        "returncode": returncode,
        "status": status,
        "stdout_file": stdout_name,
        "stdout_sha256": _sha256_bytes(stdout.encode("utf-8")),
        "stderr_file": stderr_name,
        "stderr_sha256": _sha256_bytes(stderr.encode("utf-8")),
    }
    if error is not None:
        record["error"] = error
    artifacts = (
        (evidence_directory / stdout_name, stdout),
        (evidence_directory / stderr_name, stderr),
        (evidence_directory / record_name, _canonical_json(record).decode("utf-8") + "\n"),
    )
    for path, contents in artifacts:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(contents)


def _render_baseline_ddl(
    baseline_root: Path,
    *,
    evidence_directory: Path | None = None,
    runner_path: Path | None = None,
) -> str:
    script = runner_path or baseline_root / "scripts/quality/iter2_performance_probe.py"
    argv = [
        sys.executable,
        "-s",
        str(script),
        "--render-ddl",
        "--source-root",
        str(baseline_root),
    ]
    timeout_seconds = 90
    started_at = datetime.now(timezone.utc)
    try:
        completed = subprocess.run(  # noqa: S603 -- fixed archived runner argv, shell disabled # nosec B603
            argv,
            cwd=baseline_root,
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout_seconds,
            env=_child_env("", pythonpath=baseline_root),
        )
    except subprocess.TimeoutExpired as exc:
        finished_at = datetime.now(timezone.utc)
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        _persist_ddl_render_process(
            evidence_directory,
            argv=argv,
            cwd=baseline_root,
            started_at=started_at,
            finished_at=finished_at,
            timeout_seconds=timeout_seconds,
            returncode=None,
            stdout=stdout,
            stderr=stderr,
            status="timed_out",
            error=f"{type(exc).__name__}: baseline DDL render exceeded {timeout_seconds}s",
        )
        raise RuntimeError(
            "baseline DDL render timed out; see baseline-ddl-render.process.json"
        ) from None
    except OSError as exc:
        finished_at = datetime.now(timezone.utc)
        _persist_ddl_render_process(
            evidence_directory,
            argv=argv,
            cwd=baseline_root,
            started_at=started_at,
            finished_at=finished_at,
            timeout_seconds=timeout_seconds,
            returncode=None,
            stdout="",
            stderr="",
            status="launch_error",
            error=f"{type(exc).__name__}: {exc}",
        )
        raise RuntimeError(
            "baseline DDL render could not be launched; see baseline-ddl-render.process.json"
        ) from None
    finished_at = datetime.now(timezone.utc)
    _persist_ddl_render_process(
        evidence_directory,
        argv=argv,
        cwd=baseline_root,
        started_at=started_at,
        finished_at=finished_at,
        timeout_seconds=timeout_seconds,
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        status="completed" if completed.returncode == 0 else "exited_nonzero",
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "could not render frozen HEAD ODS DDL from the isolated baseline; "
            "see baseline-ddl-render.stderr.log"
        )
    value = json.loads(completed.stdout)
    ddl = value.get("ddl") if isinstance(value, dict) else None
    if not isinstance(ddl, str) or "PRIMARY KEY (`thscode`, `trade_date`)" not in ddl:
        raise RuntimeError("frozen ODS DDL did not declare the expected stock_daily business key")
    guard = value.get("offline_guard") if isinstance(value, dict) else None
    dispatches = guard.get("original_dispatches") if isinstance(guard, dict) else None
    blocked_events = guard.get("blocked_events") if isinstance(guard, dict) else None
    known_probe_count = (
        sum(
            _DenyAllNetworkGuard._is_known_bootstrap_capability_probe(event)
            for event in blocked_events
        )
        if isinstance(blocked_events, list)
        else -1
    )
    process_guard = guard.get("process_guard") if isinstance(guard, dict) else None
    http_guard = guard.get("http_guard") if isinstance(guard, dict) else None
    if (
        not isinstance(guard, dict)
        or guard.get("mode") != "deny_all"
        or not isinstance(guard.get("blocked_attempts"), int)
        or isinstance(guard.get("blocked_attempts"), bool)
        or guard.get("blocked_attempts") != known_probe_count
        or not 0 <= known_probe_count <= 1
        or guard.get("known_bootstrap_probe_attempts") != known_probe_count
        or guard.get("unclassified_blocked_attempts") != 0
        or guard.get("all_denied_attempts_recorded") is not True
        or not isinstance(blocked_events, list)
        or len(blocked_events) != guard.get("blocked_attempts")
        or guard.get("target_dispatch_delta") != 0
        or guard.get("saved_original_dispatches") != dispatches
        or not isinstance(dispatches, dict)
        or not dispatches
        or any(type(count) is not int or count != 0 for count in dispatches.values())
        or not isinstance(process_guard, dict)
        or process_guard.get("mode") != "worker_process_guard"
        or process_guard.get("fixed_ps_attempts") != 0
        or process_guard.get("blocked_attempts") != 0
        or not process_guard.get("installed_hooks")
        or not isinstance(http_guard, dict)
        or http_guard.get("mode") != "deny_all_python_http_dispatch"
        or http_guard.get("blocked_attempts") != 0
        or not http_guard.get("installed_hooks")
    ):
        raise RuntimeError("frozen ODS DDL render lacked zero-dispatch offline guard evidence")
    return ddl


def _snapshot_worker(
    repository: Path,
    label: str,
    source_root: Path,
    source_schema: str,
    target_schema: str,
    port: int,
    password: str,
    destination: Path,
    run_id: str,
    container_id: str,
    container_name: str,
    volume_name: str,
    docker_endpoint: str,
    proof: dict[str, Any],
    *,
    preflight_only: bool = False,
) -> dict[str, Any]:
    del repository  # The live checkout must not participate in child path resolution.
    worker_script = source_root / "scripts/quality/iter2_performance_probe.py"
    if not worker_script.is_file() or proof.get("snapshot_source_root") != str(
        source_root.resolve()
    ):
        raise RuntimeError("worker launch root is not the controller-verified frozen bundle")
    command = [
        sys.executable,
        "-s",
        str(worker_script),
        "--worker",
        "--source-root",
        str(source_root),
        "--label",
        label,
        "--source-schema",
        source_schema,
        "--target-schema",
        target_schema,
        "--port",
        str(port),
        "--run-id",
        run_id,
        "--container-id",
        container_id,
        "--container-name",
        container_name,
        "--volume-name",
        volume_name,
        "--docker-endpoint",
        docker_endpoint,
        "--worker-proof-json",
        _canonical_json(proof).decode("utf-8"),
    ]
    if preflight_only:
        command.append("--preflight-only")
    result = subprocess.run(  # noqa: S603 -- fixed archived runner argv, shell disabled # nosec B603
        command,
        cwd=source_root,
        capture_output=True,
        check=False,
        text=True,
        timeout=1_800,
        env=_child_env(password, port=port, database=target_schema, pythonpath=source_root),
    )
    stdout = result.stdout.replace(password, "[REDACTED]")
    stderr = result.stderr.replace(password, "[REDACTED]")
    (destination / f"{label}-worker.stdout.json").write_text(stdout, encoding="utf-8")
    (destination / f"{label}-worker.stderr.log").write_text(stderr, encoding="utf-8")
    if result.returncode != 0:
        raise RuntimeError(f"{label} worker exited {result.returncode}; see redacted worker logs")
    try:
        snapshot = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{label} worker did not return strict JSON") from exc
    if not isinstance(snapshot, dict) or snapshot.get("label") != label:
        raise RuntimeError(f"{label} worker returned an invalid identity")
    return snapshot


def _write_json(path: Path, payload: object) -> None:
    path.write_bytes(_canonical_json(payload) + b"\n")


def _atomic_write_json(path: Path, payload: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    _write_json(temporary, payload)
    temporary.replace(path)


def _isolated_run(repository: Path, output_root: Path) -> int:
    run_id = str(uuid.uuid4())
    run_suffix = uuid.uuid4().hex[:12]
    run_dir = output_root / f"iter2-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{run_suffix}"
    run_dir.mkdir(parents=True, exist_ok=False)
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "status": "running",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "offline isolated synthetic stock_daily ODS import plus API query/export",
        "claim_limit": (
            "not production capacity, not a C65 source-fact replay, not full AC2-23 acceptance"
        ),
        "adapter_disclosure": (
            "ODS import reads the task-owned source schema with a guarded read-only SQL page "
            "adapter; "
            "query and CSV export call the existing data_query service functions directly."
        ),
        "baseline_commit": BASELINE_COMMIT,
        "image": MYSQL_IMAGE,
        "image_source": MYSQL_IMAGE_SOURCE,
        "image_platform": MYSQL_IMAGE_PLATFORM,
        "fixture_config": {
            "rows_per_symbol": ROWS_PER_SYMBOL,
            "symbol_count": SYMBOL_COUNT,
            "expected_rows": EXPECTED_ROWS,
            "seed": FIXTURE_SEED,
            "calendar": "synthetic weekdays ending 2026-10-12; not a trading calendar",
            "page_rows": PAGE_ROWS,
            "query_page_size": QUERY_PAGE_SIZE,
            "export_limit": EXPORT_LIMIT,
            "warmups": WARMUPS,
            "measured_samples": MEASURED_SAMPLES,
            "writer_mode": "staging",
            "writer_batch_size": PAGE_ROWS,
            "selected_fields": ["close_price"],
            "ods_columns": list(SOURCE_COLUMNS),
            "primary_key": list(KEY),
        },
        "lifecycle": {},
    }
    _atomic_write_json(run_dir / "manifest.json", manifest)
    resources: dict[str, Any] = {}
    before_root: Path | None = None
    current_root: Path | None = None
    snapshot_before_root: Path | None = None
    snapshot_current_root: Path | None = None
    snapshots_dir: Path | None = None
    live_hash_before: str | None = None
    live_inventory_before: dict[str, dict[str, object]] | None = None
    live_excluded_before: dict[str, int] = {}
    live_cache_counts_before: dict[str, int] = {}
    after_source_archive: dict[str, object] | None = None
    password: str | None = None
    return_code = 2
    try:
        if not str(sys.executable).startswith("/Users/yunjinqi/opt/anaconda3/"):
            raise RuntimeError("run this probe using the user's Anaconda base Python")
        commit = subprocess.run(  # noqa: S603 -- fixed git rev-parse of pinned baseline # nosec B603
            [_executable("git"), "rev-parse", BASELINE_COMMIT],
            cwd=repository,
            capture_output=True,
            check=True,
            text=True,
        ).stdout.strip()
        if commit != BASELINE_COMMIT:
            raise RuntimeError("the requested baseline commit resolved unexpectedly")
        snapshots_dir = Path(tempfile.mkdtemp(prefix="opendata-c100-perf-snapshots-"))
        snapshot_before_root, snapshot_current_root = _pinned_source_snapshot(
            repository, snapshots_dir
        )
        (
            live_hash_before,
            live_inventory_before,
            live_excluded_before,
            live_cache_counts_before,
        ) = _source_tree_state(repository / "opendata")
        before_source_archive = _persist_source_bundle(
            snapshot_before_root, run_dir, run_id, "before", run_suffix
        )
        after_source_archive = _persist_source_bundle(
            snapshot_current_root, run_dir, run_id, "after", run_suffix
        )
        before_root = _extract_verified_source_bundle(
            run_dir / str(before_source_archive["archive_file"]),
            run_dir / str(before_source_archive["manifest_file"]),
            run_dir / "source-before-input",
        )
        current_root = _extract_verified_source_bundle(
            run_dir / str(after_source_archive["archive_file"]),
            run_dir / str(after_source_archive["manifest_file"]),
            run_dir / "source-after-input",
        )
        manifest["source_trees"] = {
            "before_commit": BASELINE_COMMIT,
            "before_package_archive_tree_sha256": before_source_archive[
                "source_package_tree_sha256"
            ],
            "current_package_archive_tree_sha256": after_source_archive[
                "source_package_tree_sha256"
            ],
            "before_extracted_package_tree_sha256": _tree_sha256(before_root / "opendata"),
            "current_extracted_package_tree_sha256": _tree_sha256(current_root / "opendata"),
            "shared_runner_sha256": _sha256_file(
                current_root / "scripts/quality/iter2_performance_probe.py"
            ),
            "working_tree_before_sha256": live_hash_before,
            "working_tree_snapshot_copy_verified": True,
            "before_source_archive": before_source_archive,
            "after_source_archive": after_source_archive,
            "retained_worker_roots": {
                "before": str(before_root.relative_to(repository)),
                "after": str(current_root.relative_to(repository)),
            },
        }
        _atomic_write_json(run_dir / "manifest.json", manifest)
        ddl = _render_baseline_ddl(before_root, evidence_directory=run_dir)
        ddl_hash = _sha256_bytes(ddl.encode("utf-8"))
        manifest["ddl_sha256"] = ddl_hash
        manifest["ddl_source"] = (
            "ods_table_ddl rendered from frozen HEAD package; exact string reused for all four "
            "schemas"
        )

        docker_endpoint = _require_local_docker_endpoint()
        manifest["docker_endpoint"] = docker_endpoint
        _atomic_write_json(run_dir / "manifest.json", manifest)
        _image_available()
        docker_host = _docker(
            ["info", "--format", "{{.ServerVersion}} {{.OSType}}/{{.Architecture}}"]
        )
        manifest["docker_host"] = docker_host
        if not docker_host.endswith("linux/aarch64"):
            raise RuntimeError(
                "pinned MySQL image is only enabled for the verified linux/aarch64 host"
            )
        _atomic_write_json(run_dir / "manifest.json", manifest)
        resources = _create_mysql_container(run_id, run_suffix, run_dir)
        resources["docker_endpoint"] = docker_endpoint
        password = str(resources.pop("password"))
        manifest["lifecycle"] = {
            key: value for key, value in resources.items() if key != "container_labels"
        }
        manifest["lifecycle"]["container_labels"] = resources.get("container_labels", {})
        manifest["lifecycle"]["volume_labels"] = _require_owned_resource(
            _docker(
                ["volume", "inspect", "--format", "{{json .Labels}}", str(resources["volume_name"])]
            ),
            run_id,
            purpose=PROBE_PURPOSE,
        )
        schemas = {
            "before_source": _schema_name(run_suffix, "before_source"),
            "before_target": _schema_name(run_suffix, "before_target"),
            "after_source": _schema_name(run_suffix, "after_source"),
            "after_target": _schema_name(run_suffix, "after_target"),
        }
        manifest["lifecycle"]["schemas"] = schemas
        from sqlalchemy import create_engine
        from sqlalchemy.engine import URL

        admin_url = URL.create(
            "mysql+pymysql",
            username="root",
            password=password,
            host="127.0.0.1",
            port=int(resources["host_port"]),
            query={"charset": "utf8mb4"},
        )
        admin_engine = create_engine(admin_url, pool_pre_ping=True, pool_size=1, max_overflow=0)
        schema_engines: dict[str, Any] = {}
        try:
            for schema in schemas.values():
                _create_schema(admin_engine, schema, ddl)
                schema_engines[schema] = _engine_for_schema(admin_url, schema)
            payload_hash = synthetic_payload_sha256()
            for source_schema in (schemas["before_source"], schemas["after_source"]):
                _fixture_insert(schema_engines[source_schema])
            manifest["payload_sha256"] = payload_hash
            manifest["lifecycle"]["source_rows"] = EXPECTED_ROWS * 2
            manifest["lifecycle"]["target_rows_initial"] = 0
            _atomic_write_json(run_dir / "manifest.json", manifest)
        finally:
            for engine in schema_engines.values():
                engine.dispose()
            admin_engine.dispose()

        before = _snapshot_worker(
            repository,
            "before",
            before_root,
            schemas["before_source"],
            schemas["before_target"],
            int(resources["host_port"]),
            password,
            run_dir,
            run_id,
            str(resources["container_id"]),
            str(resources["container_name"]),
            str(resources["volume_name"]),
            str(resources["docker_endpoint"]),
            _controller_worker_proof(
                label="before",
                source_root=before_root,
                source_schema=schemas["before_source"],
                target_schema=schemas["before_target"],
                port=int(resources["host_port"]),
                run_id=run_id,
                container_id=str(resources["container_id"]),
                container_name=str(resources["container_name"]),
                volume_name=str(resources["volume_name"]),
                docker_endpoint=str(resources["docker_endpoint"]),
                source_bundle=before_source_archive,
                bundle_directory=run_dir,
                task_password=password,
            ),
        )
        if (
            before.get("source_tree_sha256")
            != manifest["source_trees"]["before_extracted_package_tree_sha256"]
        ):
            raise RuntimeError(
                "baseline worker source tree does not match the frozen archive fingerprint"
            )
        _write_json(run_dir / "before.json", before)
        after = _snapshot_worker(
            repository,
            "after",
            current_root,
            schemas["after_source"],
            schemas["after_target"],
            int(resources["host_port"]),
            password,
            run_dir,
            run_id,
            str(resources["container_id"]),
            str(resources["container_name"]),
            str(resources["volume_name"]),
            str(resources["docker_endpoint"]),
            _controller_worker_proof(
                label="after",
                source_root=current_root,
                source_schema=schemas["after_source"],
                target_schema=schemas["after_target"],
                port=int(resources["host_port"]),
                run_id=run_id,
                container_id=str(resources["container_id"]),
                container_name=str(resources["container_name"]),
                volume_name=str(resources["volume_name"]),
                docker_endpoint=str(resources["docker_endpoint"]),
                source_bundle=after_source_archive,
                bundle_directory=run_dir,
                task_password=password,
            ),
        )
        if (
            after.get("source_tree_sha256")
            != manifest["source_trees"]["current_extracted_package_tree_sha256"]
        ):
            raise RuntimeError(
                "current worker source tree does not match the captured snapshot fingerprint"
            )
        _write_json(run_dir / "after.json", after)
        comparison = _validate_comparison(before, after)
        _write_json(run_dir / "comparison.json", comparison)
        manifest["comparison"] = comparison
        if not comparison["comparable"]:
            manifest["status"] = "measured_incomplete"
        elif comparison["results_equal"]:
            manifest["status"] = "measured_comparable_results_equal"
        else:
            manifest["status"] = "measured_comparable_result_differences"
        manifest["measurements_claim"] = (
            "offline measurement only; no performance threshold or release acceptance"
        )
        return_code = 0 if comparison["comparable"] else 2
    except Exception as exc:
        manifest["status"] = "failed"
        error_text = str(exc)
        if password:
            error_text = error_text.replace(password, "[REDACTED]")
        manifest["error"] = f"{type(exc).__name__}: {error_text}"
    finally:
        manifest["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        if resources:
            try:
                manifest["lifecycle"]["cleanup"] = _cleanup_owned_resources(resources, run_id)
            except Exception as cleanup_error:
                manifest["lifecycle"]["cleanup_error"] = (
                    f"{type(cleanup_error).__name__}: {cleanup_error}"
                )
                manifest["status"] = "cleanup_failed"
                return_code = 2
        staging_evidence: list[dict[str, object]] = []
        for side, staging_root, bundle in (
            ("before", before_root, manifest.get("source_trees", {}).get("before_source_archive")),
            ("after", current_root, after_source_archive),
        ):
            if staging_root is None or not isinstance(bundle, dict):
                continue
            try:
                archive_path = run_dir / str(bundle["archive_file"])
                manifest_path = run_dir / str(bundle["manifest_file"])
                stage_receipt = _verify_staged_source_bundle(
                    archive_path, manifest_path, staging_root
                )
                _remove_readonly_tree(staging_root)
                stage_receipt["cleanup"] = "removed_after_archive_byte_and_mode_verification"
                staging_evidence.append(stage_receipt)
            except Exception as staging_error:
                manifest.setdefault("staging_cleanup_errors", []).append(
                    f"{side}: {type(staging_error).__name__}: {staging_error}"
                )
                manifest["status"] = "cleanup_failed"
                return_code = 2
        if staging_evidence:
            staging_path = run_dir / "source-staging-cleanup.json"
            if staging_path.exists():
                manifest["status"] = "cleanup_failed"
                manifest["staging_cleanup_errors"] = [
                    *manifest.get("staging_cleanup_errors", []),
                    "refusing to overwrite source-staging-cleanup.json",
                ]
                return_code = 2
            else:
                _write_json(
                    staging_path,
                    {
                        "schema_version": 1,
                        "created_at_utc": datetime.now(timezone.utc).isoformat(),
                        "claim_limit": "runtime staging verification and cleanup only",
                        "preserved_artifacts": "source tar archives and source manifests",
                        "sides": staging_evidence,
                    },
                )
                manifest["source_staging_cleanup"] = {
                    "evidence_file": staging_path.name,
                    "evidence_sha256": _sha256_file(staging_path),
                    "removed_count": len(staging_evidence),
                    "preserved_tar_and_manifest": True,
                }
        if live_inventory_before is not None and after_source_archive is not None:
            try:
                (
                    working_tree_after,
                    live_inventory_after,
                    live_excluded_after,
                    live_cache_counts_after,
                ) = _source_tree_state(repository / "opendata")
                manifest.setdefault("source_trees", {})["working_tree_after_sha256"] = (
                    working_tree_after
                )
                bundle_manifest_path = run_dir / str(after_source_archive["manifest_file"])
                bundle_manifest = json.loads(bundle_manifest_path.read_text(encoding="utf-8"))
                drift = _current_worktree_drift_evidence(
                    live_inventory_before,
                    live_inventory_after,
                    bundle_manifest,
                    live_hash_before or "",
                    working_tree_after,
                    live_excluded_before,
                    live_excluded_after,
                    live_cache_counts_before,
                    live_cache_counts_after,
                )
                drift_path = run_dir / "current-worktree-drift.json"
                if drift_path.exists():
                    raise FileExistsError("refusing to overwrite current worktree drift evidence")
                _write_json(drift_path, drift)
                manifest["current_worktree_drift"] = {
                    "has_worktree_drift": drift["has_worktree_drift"],
                    "start_matches_captured_bundle": drift["start_matches_captured_bundle"],
                    "end_matches_captured_bundle": drift["end_matches_captured_bundle"],
                    "start_to_end_source_delta_count": len(
                        _require_list(
                            drift.get("start_to_end_source_deltas"), "start-to-end deltas"
                        )
                    ),
                    "captured_bundle_to_end_source_delta_count": len(
                        _require_list(
                            drift.get("captured_bundle_to_end_source_deltas"),
                            "captured-bundle deltas",
                        )
                    ),
                    "python_cache_counts": drift["python_cache_counts"],
                    "evidence_file": drift_path.name,
                    "evidence_sha256": _sha256_file(drift_path),
                    "measurement_input": "sealed before/after archives only",
                }
                if drift["has_worktree_drift"] and manifest.get("status", "").startswith(
                    "measured_"
                ):
                    manifest["status"] = f"{manifest['status']}_with_worktree_drift"
            except Exception as drift_error:
                manifest["worktree_drift_evidence_error"] = (
                    f"{type(drift_error).__name__}: {drift_error}"
                )
                if manifest.get("status", "").startswith("measured_"):
                    manifest["status"] = f"{manifest['status']}_drift_unrecorded"
                return_code = 2
        if snapshots_dir is not None:
            shutil.rmtree(snapshots_dir, ignore_errors=True)
        manifest["result_directory"] = str(run_dir.relative_to(repository))
        _atomic_write_json(run_dir / "manifest.json", manifest)
        print(f"AC2-23 offline probe evidence: {run_dir}")
    return return_code


def _remove_readonly_tree(root: Path) -> None:
    """Remove only an archive staging root created by this run."""
    if not root.exists():
        return
    entries = sorted(root.rglob("*"), key=lambda path: len(path.parts), reverse=True)
    for path in entries:
        path.chmod(0o700 if path.is_dir() else 0o600)
    root.chmod(0o700)
    shutil.rmtree(root)


def _preflight_smoke(repository: Path, output_root: Path) -> int:
    """Run controller and archive-local worker preflight with an empty task-owned DB."""
    run_id = str(uuid.uuid4())
    run_suffix = uuid.uuid4().hex[:12]
    run_dir = output_root / (f"preflight-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{run_suffix}")
    run_dir.mkdir(parents=True, exist_ok=False)
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "status": "running",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "owned Docker MySQL and frozen worker preflight only",
        "claim_limit": "no benchmark, no synthetic payload import, no production capacity claim",
        "benchmark_started": False,
        "fixture_rows": 0,
        "baseline_commit": BASELINE_COMMIT,
        "image": MYSQL_IMAGE,
        "image_platform": MYSQL_IMAGE_PLATFORM,
        "lifecycle": {},
    }
    _atomic_write_json(run_dir / "manifest.json", manifest)
    resources: dict[str, Any] = {}
    snapshots_dir: Path | None = None
    source_root: Path | None = None
    password: str | None = None
    (
        live_hash_before,
        live_inventory_before,
        live_excluded_before,
        live_cache_counts_before,
    ) = _source_tree_state(repository / "opendata")
    manifest["working_tree_start_sha256"] = live_hash_before
    return_code = 2
    try:
        if not str(sys.executable).startswith("/Users/yunjinqi/opt/anaconda3/"):
            raise RuntimeError("run this smoke using the user's Anaconda base Python")
        commit = subprocess.run(  # noqa: S603 -- fixed git rev-parse of pinned baseline # nosec B603
            [_executable("git"), "rev-parse", BASELINE_COMMIT],
            cwd=repository,
            capture_output=True,
            check=True,
            text=True,
        ).stdout.strip()
        if commit != BASELINE_COMMIT:
            raise RuntimeError("the requested baseline commit resolved unexpectedly")

        snapshots_dir = Path(tempfile.mkdtemp(prefix="opendata-c100-perf-preflight-"))
        baseline_root, current_snapshot_root = _pinned_source_snapshot(repository, snapshots_dir)
        source_bundle = _persist_source_bundle(
            current_snapshot_root, run_dir, run_id, "after", run_suffix
        )
        source_root = _extract_verified_source_bundle(
            run_dir / str(source_bundle["archive_file"]),
            run_dir / str(source_bundle["manifest_file"]),
            run_dir / "source-after-input",
        )
        manifest["source_bundle"] = source_bundle
        manifest["source_tree_sha256"] = _tree_sha256(source_root / "opendata")
        _atomic_write_json(run_dir / "manifest.json", manifest)

        ddl = _render_baseline_ddl(baseline_root, evidence_directory=run_dir)
        manifest["ddl_sha256"] = _sha256_bytes(ddl.encode("utf-8"))
        docker_endpoint = _require_local_docker_endpoint()
        manifest["docker_endpoint"] = docker_endpoint
        _image_available()
        docker_host = _docker(
            ["info", "--format", "{{.ServerVersion}} {{.OSType}}/{{.Architecture}}"]
        )
        manifest["docker_host"] = docker_host
        if not docker_host.endswith("linux/aarch64"):
            raise RuntimeError(
                "pinned MySQL image is only enabled for the verified linux/aarch64 host"
            )
        _atomic_write_json(run_dir / "manifest.json", manifest)

        resources = _create_mysql_container(run_id, run_suffix, run_dir)
        resources["docker_endpoint"] = docker_endpoint
        password = str(resources.pop("password"))
        manifest["lifecycle"] = {
            key: value for key, value in resources.items() if key != "container_labels"
        }
        manifest["lifecycle"]["container_labels"] = resources.get("container_labels", {})
        manifest["lifecycle"]["volume_labels"] = _require_owned_resource(
            _docker(
                ["volume", "inspect", "--format", "{{json .Labels}}", str(resources["volume_name"])]
            ),
            run_id,
            purpose=PROBE_PURPOSE,
        )
        schemas = {
            "before_source": _schema_name(run_suffix, "before_source"),
            "before_target": _schema_name(run_suffix, "before_target"),
            "after_source": _schema_name(run_suffix, "after_source"),
            "after_target": _schema_name(run_suffix, "after_target"),
        }
        manifest["lifecycle"]["schemas"] = schemas
        from sqlalchemy import create_engine
        from sqlalchemy.engine import URL

        admin_url = URL.create(
            "mysql+pymysql",
            username="root",
            password=password,
            host="127.0.0.1",
            port=int(resources["host_port"]),
            query={"charset": "utf8mb4"},
        )
        admin_engine = create_engine(admin_url, pool_pre_ping=True, pool_size=1, max_overflow=0)
        try:
            for schema in schemas.values():
                _create_schema(admin_engine, schema, ddl)
        finally:
            admin_engine.dispose()
        manifest["lifecycle"]["schema_creation"] = "four task-owned empty schemas"
        _atomic_write_json(run_dir / "manifest.json", manifest)

        proof = _controller_worker_proof(
            label="after",
            source_root=source_root,
            source_schema=schemas["after_source"],
            target_schema=schemas["after_target"],
            port=int(resources["host_port"]),
            run_id=run_id,
            container_id=str(resources["container_id"]),
            container_name=str(resources["container_name"]),
            volume_name=str(resources["volume_name"]),
            docker_endpoint=docker_endpoint,
            source_bundle=source_bundle,
            bundle_directory=run_dir,
            task_password=password,
        )
        _write_json(run_dir / "controller-worker-proof.json", proof)
        worker_result = _snapshot_worker(
            repository,
            "after",
            source_root,
            schemas["after_source"],
            schemas["after_target"],
            int(resources["host_port"]),
            password,
            run_dir,
            run_id,
            str(resources["container_id"]),
            str(resources["container_name"]),
            str(resources["volume_name"]),
            docker_endpoint,
            proof,
            preflight_only=True,
        )
        _write_json(run_dir / "worker-preflight.json", worker_result)
        if worker_result.get("status") != "preflight_pass":
            raise RuntimeError("archive-local worker did not return a preflight pass")
        _validate_preflight_worker_input(worker_result, proof)
        manifest["sealed_worker_input"] = {
            "validation": "pass",
            "source_bundle_archive_sha256": proof["source_bundle_archive_sha256"],
            "source_bundle_manifest_sha256": proof["source_bundle_manifest_sha256"],
            "source_package_archive_tree_sha256": proof["source_package_archive_tree_sha256"],
            "extracted_package_tree_sha256": proof["extracted_package_tree_sha256"],
            "runner_sha256": proof["runner_sha256"],
            "pyproject_sha256": proof["pyproject_sha256"],
            "worker_echo_validation": "pass",
        }
        manifest["status"] = "preflight_pass"
        manifest["preflight_result"] = {
            "controller_worker_proof": "pass",
            "archive_local_worker": "pass",
            "mysql_target_connection": "pass",
            "sealed_input_hash_echo": "pass",
            "benchmark_started": False,
            "fixture_rows": 0,
        }
        return_code = 0
    except Exception as exc:
        manifest["status"] = "failed"
        error_text = str(exc)
        if password:
            error_text = error_text.replace(password, "[REDACTED]")
        manifest["error"] = f"{type(exc).__name__}: {error_text}"
    finally:
        manifest["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        if resources:
            try:
                manifest["lifecycle"]["cleanup"] = _cleanup_owned_resources(resources, run_id)
            except Exception as cleanup_error:
                manifest["lifecycle"]["cleanup_error"] = (
                    f"{type(cleanup_error).__name__}: {cleanup_error}"
                )
                manifest["status"] = "cleanup_failed"
                return_code = 2
        if source_root is not None:
            try:
                _remove_readonly_tree(source_root)
                manifest["staging_cleanup"] = "removed_owned_archive_extraction"
            except Exception as staging_error:
                manifest["staging_cleanup_error"] = (
                    f"{type(staging_error).__name__}: {staging_error}"
                )
                manifest["status"] = "cleanup_failed"
                return_code = 2
        if snapshots_dir is not None:
            shutil.rmtree(snapshots_dir, ignore_errors=True)
        try:
            (
                working_tree_after,
                live_inventory_after,
                live_excluded_after,
                live_cache_counts_after,
            ) = _source_tree_state(repository / "opendata")
            manifest["working_tree_after_sha256"] = working_tree_after
            if "source_bundle" in manifest:
                bundle_manifest_path = run_dir / str(manifest["source_bundle"]["manifest_file"])
                bundle_manifest = json.loads(bundle_manifest_path.read_text(encoding="utf-8"))
                drift = _current_worktree_drift_evidence(
                    live_inventory_before,
                    live_inventory_after,
                    bundle_manifest,
                    live_hash_before,
                    working_tree_after,
                    live_excluded_before,
                    live_excluded_after,
                    live_cache_counts_before,
                    live_cache_counts_after,
                )
                drift_path = run_dir / "current-worktree-drift.json"
                if drift_path.exists():
                    raise FileExistsError("refusing to overwrite current worktree drift evidence")
                _write_json(drift_path, drift)
                manifest["current_worktree_drift"] = {
                    "has_worktree_drift": drift["has_worktree_drift"],
                    "start_matches_captured_bundle": drift["start_matches_captured_bundle"],
                    "end_matches_captured_bundle": drift["end_matches_captured_bundle"],
                    "start_to_end_source_delta_count": len(
                        _require_list(
                            drift.get("start_to_end_source_deltas"), "start-to-end deltas"
                        )
                    ),
                    "captured_bundle_to_end_source_delta_count": len(
                        _require_list(
                            drift.get("captured_bundle_to_end_source_deltas"),
                            "captured-bundle deltas",
                        )
                    ),
                    "python_cache_counts": drift["python_cache_counts"],
                    "evidence_file": drift_path.name,
                    "evidence_sha256": _sha256_file(drift_path),
                    "measurement_input": "sealed archive only",
                }
                if (
                    manifest.get("sealed_worker_input", {}).get("validation") == "pass"
                    and manifest.get("status") != "cleanup_failed"
                ):
                    manifest["status"] = (
                        "preflight_pass_with_worktree_drift"
                        if drift["has_worktree_drift"]
                        else "preflight_pass"
                    )
                    manifest["preflight_result"]["live_worktree_drift"] = (
                        "recorded_separately" if drift["has_worktree_drift"] else "none_observed"
                    )
        except Exception as drift_error:
            manifest["status"] = "drift_evidence_failed"
            manifest["drift_evidence_error"] = f"{type(drift_error).__name__}: {drift_error}"
            return_code = 2
        _atomic_write_json(run_dir / "manifest.json", manifest)
        print(f"AC2-23 worker preflight smoke evidence: {run_dir}")
    return return_code


def _worker(args: argparse.Namespace) -> int:
    """Validate controller proof, install guards, then run one frozen snapshot worker."""
    proof = _validate_worker_proof(args)
    password = os.environ.get(_PASSWORD_ENV)
    if not password:
        raise RuntimeError("worker requires the ephemeral task-generated MySQL password")
    expected_environment = {
        "TESTING": "true",
        "APP_ENV": "testing",
        "MYSQL_HOST": "127.0.0.1",
        "MYSQL_PORT": str(args.port),
        "MYSQL_USER": "root",
        "MYSQL_PASSWORD": password,
        "MYSQL_DATABASE": args.target_schema,
        "DATA_MYSQL_HOST": "127.0.0.1",
        "DATA_MYSQL_PORT": str(args.port),
        "DATA_MYSQL_USER": "root",
        "DATA_MYSQL_PASSWORD": password,
        "DATA_MYSQL_DATABASE": args.target_schema,
    }
    if any(os.environ.get(name) != value for name, value in expected_environment.items()):
        raise RuntimeError("worker SQL configuration does not point solely to its task target")
    os.chdir(proof["snapshot_source_root"])
    sys.path.insert(0, proof["snapshot_source_root"])

    process_guard = _TaskProcessGuard()
    process_guard.install()
    socket_guard = _TaskSocketGuard(args.port)
    http_guard = _TaskHTTPGuard()
    event_api = None
    engine_class = None
    sql_guard = None
    try:
        socket_guard.install()
        http_guard.install()
        from sqlalchemy import event
        from sqlalchemy.engine import Engine

        event_api = event
        engine_class = Engine
        sql_guard = _TaskSQLGuard(args.source_schema, args.target_schema)
        event.listen(Engine, "before_cursor_execute", sql_guard.before_cursor_execute)
        if args.preflight_only:
            return _worker_preflight_only(
                args, process_guard, socket_guard, http_guard, event, proof
            )
        return _worker_measurement(args, process_guard, socket_guard, http_guard, event, proof)
    finally:
        if event_api is not None and engine_class is not None and sql_guard is not None:
            with suppress(Exception):
                event_api.remove(
                    engine_class, "before_cursor_execute", sql_guard.before_cursor_execute
                )
        http_guard.restore()
        socket_guard.restore()
        process_guard.restore()


def _worker_preflight_only(
    args: argparse.Namespace,
    process_guard: _TaskProcessGuard,
    socket_guard: _TaskSocketGuard,
    http_guard: _TaskHTTPGuard,
    event: _EventApi,
    proof: dict[str, Any],
) -> int:
    """Validate one frozen worker's owned MySQL endpoint without benchmark work."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import URL

    password = os.environ[_PASSWORD_ENV]
    url = URL.create(
        "mysql+pymysql",
        username="root",
        password=password,
        host="127.0.0.1",
        port=args.port,
        database=args.target_schema,
        query={"charset": "utf8mb4"},
    )
    engine = create_engine(url, pool_pre_ping=True, pool_size=1, max_overflow=0)
    counts = {"connect": 0, "checkout": 0, "checkin": 0}
    for event_name in counts:

        def increment(*_args: object, _event: str = event_name) -> None:
            counts[_event] += 1

        event.listen(engine, event_name, increment)
    try:
        with engine.connect() as connection:
            database_name, sentinel = connection.execute(text("SELECT DATABASE(), 1")).one()
        if database_name != args.target_schema or sentinel != 1:
            raise RuntimeError("worker preflight SQL result did not match its owned target schema")
        if counts["connect"] != 1 or counts["checkout"] < 1 or counts["checkin"] < 1:
            raise RuntimeError("worker preflight did not record a complete task connection")
        socket_guard.assert_no_blocked_attempts()
        process_guard.assert_no_blocked_attempts()
        http_guard.assert_no_blocked_attempts()
        if socket_guard.allowed_connection_attempts < 1:
            raise RuntimeError("worker preflight did not use the verified loopback endpoint")
        root = Path(args.source_root)
        result = {
            "label": args.label,
            "status": "preflight_pass",
            "target_schema": args.target_schema,
            "sql_sentinel": sentinel,
            "connection_counts": counts,
            "allowed_socket_attempts": socket_guard.allowed_connection_attempts,
            "source_tree_sha256": _tree_sha256(root / "opendata"),
            "runner_sha256": _sha256_file(root / "scripts/quality/iter2_performance_probe.py"),
            "frozen_input": _worker_frozen_input_claim(proof),
            "process_guard": process_guard.evidence(),
            "http_guard": http_guard.evidence(),
        }
        print(_canonical_json(result).decode("utf-8"))
        return 0
    finally:
        engine.dispose()


def _worker_measurement(
    args: argparse.Namespace,
    process_guard: _TaskProcessGuard,
    socket_guard: _TaskSocketGuard,
    http_guard: _TaskHTTPGuard,
    event: _EventApi,
    proof: dict[str, Any],
) -> int:
    """Measure one archive snapshot after process, socket, HTTP and SQL guards install."""
    password = os.environ[_PASSWORD_ENV]
    sys.path.insert(0, proof["snapshot_source_root"])
    import pandas as pd
    from sqlalchemy import create_engine, inspect, text
    from sqlalchemy.engine import URL

    from opendata.api.data_query import (
        _validated_query,
        export_domain_data,
        query_domain_data,
    )
    from opendata.api.dependencies import Principal
    from opendata.data.providers.catalog import register_provider
    from opendata.models.user import User
    from opendata.pipeline.ods_writer import OdsWriter
    from opendata.pipeline.query import EXPORT_BATCH_ROWS, DataQuery, build_data_select

    try:
        register_provider("ths")
    except Exception as exc:
        raise RuntimeError("could not register the real THS stock_daily API capability") from exc
    url_args = {
        "drivername": "mysql+pymysql",
        "username": "root",
        "password": password,
        "host": "127.0.0.1",
        "port": args.port,
        "query": {"charset": "utf8mb4"},
    }
    source_url = URL.create(database=args.source_schema, **url_args)
    target_url = URL.create(database=args.target_schema, **url_args)
    source_engine = create_engine(source_url, pool_pre_ping=True, pool_size=2, max_overflow=0)
    target_engine = create_engine(target_url, pool_pre_ping=True, pool_size=2, max_overflow=0)
    counts = {
        "source": {"connect": 0, "checkout": 0, "checkin": 0},
        "target": {"connect": 0, "checkout": 0, "checkin": 0},
    }
    for name, engine in (("source", source_engine), ("target", target_engine)):
        for event_name in counts[name]:

            def increment(*_args: object, _name: str = name, _event: str = event_name) -> None:
                counts[_name][_event] += 1

            event.listen(engine, event_name, increment)

    try:
        source_shape = _reflect_shape(source_engine, inspect)
        target_shape = _reflect_shape(target_engine, inspect)
        if source_shape != target_shape:
            raise RuntimeError("source/target reflected schemas differ")
        if source_shape["primary_key"] != list(KEY):
            raise RuntimeError("ODS business key differs from the expected THS key")
        source_ddl_text = _show_create(source_engine, text)
        target_ddl_text = _show_create(target_engine, text)
        if source_ddl_text != target_ddl_text:
            raise RuntimeError("source/target SHOW CREATE TABLE DDL differs")
        ddl_hash = _sha256_bytes(target_ddl_text.encode("utf-8"))
        payload_hash = synthetic_payload_sha256()
        import_runs: list[dict[str, Any]] = []
        writer = OdsWriter(target_engine, batch_size=PAGE_ROWS, mode="staging")
        count_before_import = _counter_copy(counts)
        for run_index, kind in enumerate(("cold", "idempotent_replay_1", "idempotent_replay_2")):
            target_before = _row_count(target_engine, text)
            started = time.perf_counter()
            rows_written = 0
            batches_written = 0
            readonly_verified = False
            with _readonly_source(source_engine, event, text) as connection:
                readonly_verified = True
                selected = ", ".join(
                    f"`{column}`"
                    for column in [*SOURCE_COLUMNS, "_source", "_fetched_at", "_batch_id"]
                )
                order = ", ".join(f"`{column}`" for column in KEY)
                source_cursor = connection.execution_options(
                    stream_results=True, max_row_buffer=PAGE_ROWS
                ).execute(
                    text(
                        f"SELECT {selected} FROM `{TABLE}` ORDER BY {order}"  # noqa: S608 -- identifiers are constants # nosec B608
                    )
                )
                try:
                    while True:
                        raw_rows = source_cursor.mappings().fetchmany(PAGE_ROWS)
                        if not raw_rows:
                            break
                        frame = pd.DataFrame.from_records([dict(row) for row in raw_rows])
                        result_write = writer.write(
                            frame,
                            table=TABLE,
                            key=KEY,
                            source="ths",
                            batch_id=FIXED_BATCH_ID,
                            fetched_at=FIXED_FETCHED_AT,
                        )
                        rows_written += result_write.rows
                        batches_written += result_write.batches
                finally:
                    source_cursor.close()
            elapsed = time.perf_counter() - started
            target_after = _row_count(target_engine, text)
            if rows_written != EXPECTED_ROWS or target_after != EXPECTED_ROWS:
                raise RuntimeError(
                    f"{kind} import did not materialize exactly {EXPECTED_ROWS} rows"
                )
            import_runs.append(
                {
                    "kind": kind,
                    "elapsed_seconds": elapsed,
                    "rows_read": rows_written,
                    "rows_written": rows_written,
                    "batches_written": batches_written,
                    "target_rows_before": target_before,
                    "target_rows_after": target_after,
                    "target_business_sha256": _business_hash(target_engine, text),
                    "source_readonly_verified": readonly_verified,
                    "process_current_rss_bytes": _current_rss_bytes(),
                    "process_peak_rss_bytes": _peak_rss_bytes(),
                }
            )
            if run_index == 0 and target_before != 0:
                raise RuntimeError("cold import target was not initially empty")
            if run_index > 0 and target_before != EXPECTED_ROWS:
                raise RuntimeError("idempotent import did not start from the complete prior target")

        import_connection_counts = {
            engine_name: {
                metric: counts[engine_name][metric] - count_before_import[engine_name][metric]
                for metric in counts[engine_name]
            }
            for engine_name in counts
        }
        fixture_start, fixture_end = _fixture_window()
        principal = Principal(
            user=User(
                username="c100_perf_probe",
                email="c100-perf-probe@example.invalid",
                hashed_password="synthetic-unused",  # noqa: S106 -- offline synthetic identity only # nosec B106
            ),
            scopes=("stock_daily",),
        )

        query_arguments: dict[str, object] = {
            "symbols": None,
            "start": fixture_start,
            "end": fixture_end,
            "source": "ths",
            "layer": "ods",
            "adjust": "none",
            "fields": "close_price",
            "period": None,
            "report_date": None,
            "page": 1,
            "page_size": QUERY_PAGE_SIZE,
            "engine": target_engine,
        }
        optional_none_arguments = ("as_of", "market")
        optional_arguments, supported_optional_none, omitted_optional_none = (
            _optional_none_signature_arguments(query_domain_data, optional_none_arguments)
        )
        query_arguments.update(optional_arguments)
        query_route: Callable[..., Awaitable[APIResponse]] = query_domain_data

        async def query_call() -> dict[str, object]:
            response = await query_route(
                "equity",
                "stock_daily",
                principal,
                **query_arguments,
            )
            payload = response.model_dump(mode="json")
            if not isinstance(payload, dict):
                raise RuntimeError("query API response did not serialize to an object")
            return {str(key): value for key, value in payload.items()}

        async def export_call() -> tuple[int, str]:
            response = await export_domain_data(
                "equity",
                "stock_daily",
                principal,
                symbols=None,
                start=fixture_start,
                end=fixture_end,
                source="ths",
                layer="ods",
                adjust="none",
                fields="close_price",
                period=None,
                report_date=None,
                limit=EXPORT_LIMIT,
                engine=target_engine,
            )
            digest = hashlib.sha256()
            byte_count = 0
            newline_count = 0
            async for fragment in response.body_iterator:
                content = fragment if isinstance(fragment, bytes) else str(fragment).encode("utf-8")
                digest.update(content)
                byte_count += len(content)
                newline_count += content.count(b"\n")
            return max(0, newline_count - 1), digest.hexdigest()

        async def explain_call() -> dict[str, Any]:
            plan = await _validated_query(
                target_engine,
                principal,
                asset_class="equity",
                domain="stock_daily",
                symbols=None,
                start=fixture_start,
                end=fixture_end,
                source="ths",
                layer="ods",
                adjust="none",
                fields="close_price",
                period=None,
                report_date=None,
                page=1,
                page_size=QUERY_PAGE_SIZE,
            )
            query_sql, query_params = build_data_select(
                plan.query,
                table=plan.table,
                columns=plan.columns,
                key=plan.key,
                time_field=plan.time_field,
                symbol_field=plan.symbol_field,
                period_field=plan.period_field,
                report_date_field=plan.report_date_field,
            )
            export_query = DataQuery(
                domain="stock_daily",
                layer="ods",
                source="ths",
                start=fixture_start,
                end=fixture_end,
                fields=("close_price",),
                page=1,
                page_size=EXPORT_BATCH_ROWS,
                adjust="none",
            )
            export_sql, export_params = build_data_select(
                export_query,
                table=plan.table,
                columns=plan.columns,
                key=plan.key,
                time_field=plan.time_field,
                symbol_field=plan.symbol_field,
                period_field=plan.period_field,
                report_date_field=plan.report_date_field,
                offset=0,
                max_rows=EXPORT_BATCH_ROWS,
            )
            with target_engine.connect() as connection:
                query_plan = str(
                    connection.execute(
                        text("EXPLAIN FORMAT=JSON " + query_sql), query_params
                    ).scalar_one()
                )
                export_plan = str(
                    connection.execute(
                        text("EXPLAIN FORMAT=JSON " + export_sql), export_params
                    ).scalar_one()
                )
            return {
                "query_sql_sha256": _sha256_bytes(query_sql.encode("utf-8")),
                "query_parameters_sha256": _sha256_bytes(_canonical_json(query_params)),
                "query_plan": query_plan,
                "query_plan_sha256": _sha256_bytes(query_plan.encode("utf-8")),
                "export_sql_sha256": _sha256_bytes(export_sql.encode("utf-8")),
                "export_parameters_sha256": _sha256_bytes(_canonical_json(export_params)),
                "export_plan": export_plan,
                "export_plan_sha256": _sha256_bytes(export_plan.encode("utf-8")),
            }

        explain = asyncio.run(explain_call())
        count_before_query = _counter_copy(counts)
        query_metrics = _measure_async_family(query_call, "query")
        query_metrics["connection_counts"] = _counter_delta(count_before_query, counts, "target")
        count_before_export = _counter_copy(counts)
        export_metrics = _measure_async_family(export_call, "export")
        export_metrics["connection_counts"] = _counter_delta(count_before_export, counts, "target")
        source_rows = _row_count(source_engine, text)
        if source_rows != EXPECTED_ROWS:
            raise RuntimeError("source row count changed during imports")
        result = {
            "label": args.label,
            "baseline_commit": BASELINE_COMMIT if args.label == "before" else None,
            "source_tree_sha256": _tree_sha256(Path(args.source_root) / "opendata"),
            "frozen_input": {
                "archive_root": proof["snapshot_source_root"],
                "source_package_archive_tree_sha256": proof["source_package_archive_tree_sha256"],
                "extracted_package_tree_sha256": proof["extracted_package_tree_sha256"],
                "source_bundle_archive_sha256": proof["source_bundle_archive_sha256"],
                "source_bundle_manifest_sha256": proof["source_bundle_manifest_sha256"],
                "runner_sha256": proof["runner_sha256"],
                "pyproject_sha256": proof["pyproject_sha256"],
            },
            "payload_sha256": payload_hash,
            "fixture_config": {
                "rows_per_symbol": ROWS_PER_SYMBOL,
                "symbol_count": SYMBOL_COUNT,
                "expected_rows": EXPECTED_ROWS,
                "seed": FIXTURE_SEED,
                "page_rows": PAGE_ROWS,
                "query_page_size": QUERY_PAGE_SIZE,
                "export_limit": EXPORT_LIMIT,
                "warmups": WARMUPS,
                "measured_samples": MEASURED_SAMPLES,
                "writer_mode": "staging",
                "writer_batch_size": PAGE_ROWS,
            },
            "ddl_sha256": ddl_hash,
            "schema_shape": {
                "source": source_shape,
                "target": target_shape,
                "source_equals_target": source_shape == target_shape,
                "source_ddl_sha256": _sha256_bytes(source_ddl_text.encode("utf-8")),
                "target_ddl_sha256": ddl_hash,
            },
            "import_runs": import_runs,
            "import_connection_counts": import_connection_counts,
            "measurements": {"query": query_metrics, "export": export_metrics},
            "connection_counts": {
                "source": counts["source"],
                "target": counts["target"],
            },
            "source_readonly_verified": all(run["source_readonly_verified"] for run in import_runs),
            "source_rows": source_rows,
            "target_rows": _row_count(target_engine, text),
            "target_business_sha256": _business_hash(target_engine, text),
            "explain": explain,
            "connection_guard": {
                "allowed_destination": f"127.0.0.1:{args.port}",
                "allowed_connection_attempts": socket_guard.allowed_connection_attempts,
                "blocked_attempts": socket_guard.blocked_attempts,
                "docker_endpoint": proof["docker_endpoint"],
                "container_id": proof["container_id"],
                "container_name": proof["container_name"],
                "volume_name": proof["volume_name"],
                "schemas": proof["schemas"],
                "verified_task_schemas": proof["verified_task_schemas"],
                "sql_guard_schemas": {
                    "source": args.source_schema,
                    "target": args.target_schema,
                },
            },
            "process_guard": process_guard.evidence(),
            "http_guard": http_guard.evidence(),
            "api_route_signature_adaptation": {
                "route": "query_domain_data",
                "inspected_at_runtime": True,
                "optional_none_arguments_passed": supported_optional_none,
                "optional_none_arguments_omitted": omitted_optional_none,
            },
            "process_peak_rss_bytes": _peak_rss_bytes(),
            "python": {
                "implementation": platform.python_implementation(),
                "version": platform.python_version(),
                "executable": sys.executable,
            },
        }
        socket_guard.assert_no_blocked_attempts()
        process_guard.assert_no_blocked_attempts()
        http_guard.assert_no_blocked_attempts()
        if socket_guard.allowed_connection_attempts == 0:
            raise RuntimeError("worker made no verified task MySQL connection attempts")
        print(_canonical_json(result).decode("utf-8"))
        return 0
    finally:
        source_engine.dispose()
        target_engine.dispose()


def _fixture_window() -> tuple[date, date]:
    dates = _synthetic_dates()
    return dates[0], dates[-1]


def _reflect_shape(
    engine: Engine, inspector_factory: Callable[[Engine], Inspector]
) -> dict[str, Any]:
    inspector = inspector_factory(engine)
    columns = sorted(
        (
            str(item["name"]),
            str(item["type"]).casefold(),
            bool(item.get("nullable", True)),
        )
        for item in inspector.get_columns(TABLE)
    )
    primary = inspector.get_pk_constraint(TABLE).get("constrained_columns") or []
    return {"columns": columns, "primary_key": list(primary)}


def _show_create(engine: Engine, text_factory: Callable[[str], TextClause]) -> str:
    with engine.connect() as connection:
        rows = connection.execute(text_factory(f"SHOW CREATE TABLE `{TABLE}`")).one()
    return str(rows[1])


def _row_count(engine: Engine, text_factory: Callable[[str], TextClause]) -> int:
    with engine.connect() as connection:
        return int(
            connection.execute(
                text_factory(f"SELECT COUNT(*) FROM `{TABLE}`")  # noqa: S608 -- fixed table identifier # nosec B608
            ).scalar_one()
        )


def _business_hash(engine: Engine, text_factory: Callable[[str], TextClause]) -> str:
    digest = hashlib.sha256()
    selected = ", ".join(f"`{column}`" for column in BUSINESS_COLUMNS)
    order = ", ".join(f"`{column}`" for column in KEY)
    with engine.connect() as connection:
        result = connection.execution_options(
            stream_results=True, max_row_buffer=PAGE_ROWS
        ).execute(
            text_factory(
                f"SELECT {selected} FROM `{TABLE}` ORDER BY {order}"  # noqa: S608 -- identifiers are constants # nosec B608
            )
        )
        try:
            while rows := result.fetchmany(PAGE_ROWS):
                for row in rows:
                    normalized = [
                        value.isoformat() if isinstance(value, date) else value for value in row
                    ]
                    digest.update(_canonical_json(normalized))
                    digest.update(b"\n")
        finally:
            result.close()
    return digest.hexdigest()


@contextmanager
def _readonly_source(
    engine: Engine,
    event_api: _EventApi,
    text_factory: Callable[[str], TextClause],
) -> Iterator[Connection]:
    def guard(_conn: object, _cursor: object, statement: str, *_args: object) -> None:
        first = statement.lstrip().split(None, 1)[0].upper() if statement.strip() else ""
        if first not in {"SELECT", "SET", "SHOW", "EXPLAIN"}:
            raise PermissionError("synthetic source transaction blocked a non-read SQL statement")

    event_api.listen(engine, "before_cursor_execute", guard)
    try:
        with engine.connect() as connection:
            raw = connection.connection.driver_connection
            if raw is None:
                raise RuntimeError("source DBAPI connection is unavailable")
            cursor = raw.cursor()
            try:
                cursor.execute("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                cursor.execute("SET SESSION TRANSACTION READ ONLY")
                cursor.execute(
                    "SELECT @@session.transaction_read_only, @@session.transaction_isolation"
                )
                state = cursor.fetchone()
            finally:
                cursor.close()
            raw.commit()
            if state is None or str(state[0]) not in {"1", "ON", "on", "True", "true"}:
                raise RuntimeError("MySQL source session did not confirm read-only mode")
            isolation = str(state[1]).replace("_", "-").casefold()
            if isolation != "repeatable-read":
                raise RuntimeError("MySQL source session did not confirm repeatable-read isolation")
            with connection.begin():
                yield connection
    finally:
        with suppress(Exception):
            event_api.remove(engine, "before_cursor_execute", guard)


def _counter_copy(counts: dict[str, dict[str, int]]) -> dict[str, dict[str, int]]:
    return {name: dict(values) for name, values in counts.items()}


def _counter_delta(
    start: dict[str, dict[str, int]],
    end: dict[str, dict[str, int]],
    engine_name: str,
) -> dict[str, int]:
    return {name: end[engine_name][name] - start[engine_name][name] for name in end[engine_name]}


def _current_rss_bytes() -> int | None:
    ps = next(
        (candidate for candidate in ("/bin/ps", "/usr/bin/ps") if Path(candidate).is_file()), None
    )
    if ps is None:
        return None
    result = subprocess.run(  # noqa: S603 -- absolute ps path and exact argv validated # nosec B603
        [ps, "-o", "rss=", "-p", str(os.getpid())],
        capture_output=True,
        text=True,
        timeout=2,
    )
    if result.returncode != 0 or not result.stdout.strip().isdigit():
        return None
    return int(result.stdout.strip()) * 1024


def _peak_rss_bytes() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if sys.platform == "darwin" else value * 1024)


def _measure_async_family(function: Callable[[], Awaitable[object]], family: str) -> dict[str, Any]:
    async def execute() -> dict[str, Any]:
        for _ in range(WARMUPS):
            await function()
        durations: list[float] = []
        hashes: list[str] = []
        row_counts: list[int] = []
        current_rss: list[int] = []
        peak_rss: list[int] = []
        for _ in range(MEASURED_SAMPLES):
            started = time.perf_counter()
            value = await function()
            durations.append(time.perf_counter() - started)
            if family == "query":
                if not isinstance(value, dict):
                    raise RuntimeError("query service returned a non-object response")
                data = value.get("data")
                if not isinstance(data, dict) or not isinstance(data.get("rows"), list):
                    raise RuntimeError("query service returned a malformed row payload")
                result_hash = _sha256_bytes(_canonical_json(value))
                rows = len(data["rows"])
            else:
                if (
                    not isinstance(value, tuple)
                    or len(value) != 2
                    or not isinstance(value[0], int)
                    or not isinstance(value[1], str)
                ):
                    raise RuntimeError("export service returned a malformed digest/count pair")
                rows, result_hash = value
            hashes.append(result_hash)
            row_counts.append(rows)
            rss = _current_rss_bytes()
            if rss is not None:
                current_rss.append(rss)
            peak_rss.append(_peak_rss_bytes())
        if len(set(hashes)) != 1 or len(set(row_counts)) != 1:
            raise RuntimeError(f"{family} output changed during repeated identical requests")
        expected = QUERY_PAGE_SIZE if family == "query" else EXPORT_LIMIT
        if row_counts[0] != expected:
            raise RuntimeError(f"{family} returned {row_counts[0]} rows; expected {expected}")
        return {
            "warmup_count": WARMUPS,
            "measured_count": MEASURED_SAMPLES,
            "samples_seconds": durations,
            "p95_seconds": _p95(durations),
            "median_seconds": sorted(durations)[len(durations) // 2],
            "minimum_seconds": min(durations),
            "maximum_seconds": max(durations),
            "result_sha256": hashes[0],
            "result_row_count": row_counts[0],
            "process_current_rss_min_bytes": min(current_rss) if current_rss else None,
            "process_current_rss_max_bytes": max(current_rss) if current_rss else None,
            "process_peak_rss_max_bytes": max(peak_rss),
        }

    return asyncio.run(execute())


def _render_ddl(source_root: Path) -> None:
    network_guard = _DenyAllNetworkGuard()
    process_guard = _TaskProcessGuard(allow_fixed_ps=False)
    http_guard = _TaskHTTPGuard()
    resolved_root = str(source_root.resolve())
    try:
        network_guard.install()
        process_guard.install()
        network_guard.set_phase("http_guard_bootstrap")
        http_guard.install()
        sys.path.insert(0, resolved_root)
        network_guard.set_phase("product_bootstrap")
        from opendata.pipeline.ddl import Column, ods_table_ddl

        daily_columns = [
            Column("thscode", "varchar(32)", nullable=False),
            Column("trade_date", "date", nullable=False),
            Column("date_ms", "bigint", nullable=False),
            Column("currency", "varchar(8)"),
            Column("interval", "varchar(8)"),
            Column("adjusted", "varchar(16)"),
            Column("open_price", "double"),
            Column("high_price", "double"),
            Column("low_price", "double"),
            Column("close_price", "double"),
            Column("volume", "double"),
            Column("turnover", "double"),
        ]
        network_guard.set_phase("ddl_render")
        ddl = ods_table_ddl(
            "stock_daily",
            "ths",
            daily_columns,
            key=KEY,
            partition_key="trade_date",
            start_year=2015,
            years=12,
        )
        evidence = network_guard.evidence()
        events = network_guard.blocked_events
        known_probe_count = sum(
            network_guard._is_known_bootstrap_capability_probe(event) for event in events
        )
        blocked_attempts = network_guard.blocked_attempts
        evidence["known_bootstrap_probe_attempts"] = known_probe_count
        evidence["unclassified_blocked_attempts"] = blocked_attempts - known_probe_count
        evidence["all_denied_attempts_recorded"] = len(events) == blocked_attempts
        evidence["process_guard"] = process_guard.evidence()
        evidence["http_guard"] = http_guard.evidence()
        evidence["saved_original_dispatches"] = dict(
            sorted(network_guard.original_dispatches.items())
        )
        original_dispatches = network_guard.original_dispatches
        target_dispatch_delta = sum(original_dispatches.values())
        evidence["target_dispatch_delta"] = target_dispatch_delta
        if (
            evidence["unclassified_blocked_attempts"] != 0
            or not evidence["all_denied_attempts_recorded"]
            or target_dispatch_delta != 0
            or any(original_dispatches.values())
            or process_guard.blocked_attempts != 0
            or http_guard.blocked_attempts != 0
        ):
            raise RuntimeError(
                "DDL renderer observed a denied dispatch attempt; guard evidence: "
                f"{_canonical_json(evidence).decode('utf-8')}"
            )
        print(_canonical_json({"ddl": ddl, "offline_guard": evidence}).decode("utf-8"))
    finally:
        http_guard.restore()
        process_guard.restore()
        network_guard.restore()
        with suppress(ValueError):
            sys.path.remove(resolved_root)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--isolated-run",
        action="store_true",
        help="create a task-owned Docker MySQL container and run the synthetic comparison",
    )
    parser.add_argument(
        "--preflight-smoke",
        action="store_true",
        help="create an owned MySQL test resource and verify frozen worker preflight only",
    )
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--preflight-only", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--render-ddl", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--source-root", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--label", choices=("before", "after"), help=argparse.SUPPRESS)
    parser.add_argument("--source-schema", help=argparse.SUPPRESS)
    parser.add_argument("--target-schema", help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--run-id", help=argparse.SUPPRESS)
    parser.add_argument("--container-id", help=argparse.SUPPRESS)
    parser.add_argument("--container-name", help=argparse.SUPPRESS)
    parser.add_argument("--volume-name", help=argparse.SUPPRESS)
    parser.add_argument("--docker-endpoint", help=argparse.SUPPRESS)
    parser.add_argument("--worker-proof-json", help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch inert CLI, isolated controller, or isolated snapshot worker."""
    args = _parser().parse_args(argv)
    if args.preflight_only and not args.worker:
        raise SystemExit("--preflight-only is valid only for an archive-local worker")
    if args.preflight_smoke and (args.isolated_run or args.worker or args.render_ddl):
        raise SystemExit("--preflight-smoke cannot be combined with another execution mode")
    if args.render_ddl:
        if args.source_root is None:
            raise SystemExit("--render-ddl requires --source-root")
        source_root = args.source_root.resolve()
        if (
            Path(__file__).resolve() != source_root / "scripts/quality/iter2_performance_probe.py"
            or Path.cwd().resolve() != source_root
            or os.environ.get("PYTHONPATH") != str(source_root)
        ):
            raise SystemExit("--render-ddl must execute from its retained archive-local runner")
        _render_ddl(args.source_root)
        return 0
    if args.worker:
        required = (
            args.source_root,
            args.label,
            args.source_schema,
            args.target_schema,
            args.port,
            args.run_id,
            args.container_id,
            args.container_name,
            args.volume_name,
            args.docker_endpoint,
            args.worker_proof_json,
        )
        if any(value is None for value in required):
            raise SystemExit("worker mode is missing a required argument")
        return _worker(args)
    if args.preflight_smoke:
        repository = Path(__file__).resolve().parents[2]
        output_root = args.output_dir or (
            repository / "docs/evidence/C100/performance/preflight-smokes"
        )
        if not output_root.is_absolute():
            output_root = (repository / output_root).resolve()
        if repository not in output_root.parents and output_root != repository:
            raise SystemExit("--output-dir must stay inside the repository")
        return _preflight_smoke(repository, output_root)
    if not args.isolated_run:
        print("No action taken. Pass --isolated-run to create a task-owned Docker MySQL instance.")
        return 2
    repository = Path(__file__).resolve().parents[2]
    output_dir = args.output_dir or repository / "docs/evidence/C100/performance/runs"
    if not output_dir.is_absolute():
        output_dir = (repository / output_dir).resolve()
    if repository not in output_dir.parents and output_dir != repository:
        raise SystemExit("--output-dir must stay inside the repository")
    return _isolated_run(repository, output_dir)


if __name__ == "__main__":
    raise SystemExit(main())
