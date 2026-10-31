"""Private loopback fixture replaying one previously captured stock-daily response.

Run this file as a subprocess with explicit local ``--raw-body``,
``--raw-body-sha256``, ``--observed-at``, ``--capture-metadata``, and
``--ready-file`` arguments inside a private temporary directory. It verifies
the captured bytes and metadata, builds a synthetic instrument-reference
snapshot for ``600519.SH`` covering the captured window, executes the reviewed
offline stock-daily backfill path into a disposable SQLite ledger/publication
database, and mounts only the real stock-daily v2 router over that publication.

The captured body is a previously recorded real provider response. This
fixture does not contact the provider, use a production database, read
``.env``, or perform any live network request. The instrument lifecycle,
alias, grants, contract, principal, capacity attestation, and API key are
synthetic test inputs.
"""

from __future__ import annotations

import argparse
import asyncio
import errno
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import signal
import socket
import stat
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from collections.abc import Sequence

_FIXTURE_HOST = "127.0.0.1"
_FIXTURE_DATASET_ID = "cn-equity-stock-daily"
_FIXTURE_SOURCE_ID = "akshare.daily"
_FIXTURE_SEMANTIC_VERSION = "stock-daily-raw-v1"
_FIXTURE_SOURCE_MARKET = "cn"
_FIXTURE_SYMBOL = "600519"
_FIXTURE_PROVIDER_SYMBOL = "600519.SH"
_FIXTURE_START = date(2026, 10, 12)
_FIXTURE_END = date(2026, 10, 23)
_FIXTURE_END_EXCLUSIVE = date(2026, 10, 24)
_FIXTURE_DATES: tuple[date, ...] = (
    date(2026, 10, 12),
    date(2026, 10, 13),
    date(2026, 10, 14),
    date(2026, 10, 15),
    date(2026, 10, 16),
    date(2026, 10, 19),
    date(2026, 10, 20),
    date(2026, 10, 21),
    date(2026, 10, 22),
    date(2026, 10, 23),
)
_FIXTURE_ROW_COUNT = 10
_FIXTURE_PAGE_SIZE = 1_000
_MAX_SAMPLE_BYTES = 2 * 1024 * 1024
_MAX_CAPTURE_METADATA_BYTES = 64 * 1024
_STARTUP_TIMEOUT_SECONDS = 20.0
_SHUTDOWN_TIMEOUT_SECONDS = 8.0


@dataclass(slots=True)
class _FixtureRuntime:
    """Hold all resources owned by one captured-response replay process."""

    app: Any
    engine: Any
    publication_id: str
    content_sha256: str
    record_count: int
    api_key: str = field(repr=False)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Parse the explicit non-secret local inputs for one replay."""
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument(
        "--raw-body",
        required=True,
        type=Path,
        help="local captured response body (never copied or printed)",
    )
    parser.add_argument(
        "--raw-body-sha256",
        required=True,
        help="expected lowercase SHA-256 of the exact captured body bytes",
    )
    parser.add_argument(
        "--observed-at",
        required=True,
        help="explicit timezone-aware observation time from the capture metadata",
    )
    parser.add_argument(
        "--capture-metadata",
        required=True,
        type=Path,
        help="local capture metadata JSON that must match the body",
    )
    parser.add_argument(
        "--ready-file",
        required=True,
        type=Path,
        help="private path used to publish loopback endpoint metadata once ready",
    )
    return parser.parse_args(argv)


def _validate_ready_path(path: Path) -> Path:
    """Require a new readiness file inside an owner-only temporary directory."""
    if not path.is_absolute() or path.exists() or path.is_symlink():
        raise ValueError("invalid fixture readiness path")
    parent = path.parent.resolve(strict=True)
    parent_stat = parent.stat()
    if not stat.S_ISDIR(parent_stat.st_mode) or parent_stat.st_mode & 0o077:
        raise ValueError("fixture readiness directory must be private")
    return parent / path.name


def _configure_isolated_environment(state_directory: Path) -> None:
    """Remove inherited credentials and point every DB setting at test-only state."""
    inherited = os.environ
    allowed_names = (
        "PATH",
        "LANG",
        "LC_ALL",
        "TZ",
        "SYSTEMROOT",
        "WINDIR",
        "TEMP",
        "TMP",
        "TMPDIR",
    )
    safe_environment = {name: inherited[name] for name in allowed_names if name in inherited}
    os.environ.clear()
    os.environ.update(safe_environment)
    os.environ.update(
        {
            "APP_ENV": "testing",
            "TESTING": "true",
            "REDIS_URL": "",
            "SENTRY_DSN": "",
            "ENABLE_SCHEDULER": "false",
            "SCHEDULER_BOOTSTRAP_ON_STARTUP": "false",
            "PATROL_ENABLED": "false",
            "HOST": _FIXTURE_HOST,
            "PORT": "0",
            "MYSQL_HOST": "fixture-db-disabled.invalid",
            "MYSQL_PORT": "1",
            "MYSQL_USER": "fixture_disabled",
            "MYSQL_PASSWORD": "",
            "MYSQL_DATABASE": "fixture_disabled",
            "DATA_MYSQL_HOST": "fixture-db-disabled.invalid",
            "DATA_MYSQL_PORT": "1",
            "DATA_MYSQL_USER": "fixture_disabled",
            "DATA_MYSQL_PASSWORD": "",
            "DATA_MYSQL_DATABASE": "fixture_disabled",
            "DATABASE_URL": f"sqlite+aiosqlite:///{state_directory / 'auth.sqlite'}",
            "DATA_DATABASE_URL": f"sqlite:///{state_directory / 'data.sqlite'}",
            "API_KEY_PEPPER": secrets.token_urlsafe(32),
            "API_KEY_FAILURE_DELAY_SECONDS": "0",
            "RATE_LIMIT_PER_MINUTE": "1000",
            "CACHE_DIR": str(state_directory / "cache"),
            "XDG_CACHE_HOME": str(state_directory / "cache"),
        }
    )
    os.chdir(state_directory)


def _is_exact_fixture_address(address: object, port: int) -> bool:
    """Return whether an IPv4 socket address targets this fixture listener."""
    return (
        isinstance(address, tuple)
        and len(address) >= 2
        and address[0] == _FIXTURE_HOST
        and type(address[1]) is int
        and address[1] == port
    )


def _is_fixture_loopback_peer(address: object) -> bool:
    """Accept only an IPv4 loopback peer on a valid TCP/UDP port."""
    if not isinstance(address, tuple) or len(address) < 2:
        return False
    try:
        numeric_host = ipaddress.ip_address(address[0])
    except (TypeError, ValueError):
        return False
    return (
        numeric_host.version == 4
        and str(numeric_host) == _FIXTURE_HOST
        and type(address[1]) is int
        and 0 < address[1] <= 65535
    )


def _install_loopback_egress_guard(listener: socket.socket) -> None:
    """Block DNS and all socket egress except the exact loopback fixture port."""
    bound_address = listener.getsockname()
    if not _is_exact_fixture_address(bound_address, bound_address[1]):
        raise ValueError("fixture listener must use an IPv4 loopback address")
    port = bound_address[1]

    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_sendto = socket.socket.sendto
    original_sendmsg = socket.socket.sendmsg
    original_getaddrinfo = socket.getaddrinfo
    original_bind = socket.socket.bind

    def guarded_connect(sock: socket.socket, address: object) -> None:
        if not _is_exact_fixture_address(address, port):
            raise OSError(errno.EPERM, "fixture network access is restricted")
        original_connect(sock, address)

    def guarded_connect_ex(sock: socket.socket, address: object) -> int:
        if not _is_exact_fixture_address(address, port):
            return errno.EPERM
        return original_connect_ex(sock, address)

    def guarded_sendto(
        sock: socket.socket,
        data: bytes,
        *args: object,
    ) -> int:
        address = args[-1] if args else None
        if not _is_exact_fixture_address(address, port):
            raise OSError(errno.EPERM, "fixture network access is restricted")
        return original_sendto(sock, data, *args)

    def guarded_sendmsg(sock: socket.socket, *args: Any, **kwargs: Any) -> int:
        address = kwargs.get("address")
        if address is None and len(args) >= 4:
            address = args[3]
        if address is None:
            try:
                peer_address = sock.getpeername()
            except OSError as exc:
                raise OSError(errno.EPERM, "fixture network access is restricted") from exc
            if not _is_fixture_loopback_peer(peer_address):
                raise OSError(errno.EPERM, "fixture network access is restricted")
        elif not _is_exact_fixture_address(address, port):
            raise OSError(errno.EPERM, "fixture network access is restricted")
        return original_sendmsg(sock, *args, **kwargs)

    def guarded_name_resolution(*args: Any, **kwargs: Any) -> Any:
        """Reject resolver entrypoints that could perform DNS or reverse lookup."""
        del args, kwargs
        raise OSError(errno.EPERM, "fixture DNS access is restricted")

    def guarded_getaddrinfo(
        host: object,
        requested_port: object,
        *args: object,
        **kwargs: object,
    ) -> list[tuple[Any, ...]]:
        try:
            numeric_host = ipaddress.ip_address(host)
        except (TypeError, ValueError) as exc:
            raise OSError(errno.EPERM, "fixture DNS access is restricted") from exc
        if (
            not numeric_host.is_loopback
            or str(numeric_host) != _FIXTURE_HOST
            or requested_port != port
        ):
            raise OSError(errno.EPERM, "fixture network access is restricted")
        return original_getaddrinfo(host, requested_port, *args, **kwargs)

    def guarded_bind(sock: socket.socket, address: object) -> None:
        if sock is not listener or not _is_exact_fixture_address(address, port):
            raise OSError(errno.EPERM, "fixture listener binding is restricted")
        original_bind(sock, address)

    socket.socket.connect = guarded_connect  # type: ignore[method-assign]
    socket.socket.connect_ex = guarded_connect_ex  # type: ignore[method-assign]
    socket.socket.sendto = guarded_sendto  # type: ignore[method-assign]
    socket.socket.sendmsg = guarded_sendmsg  # type: ignore[method-assign]
    socket.getaddrinfo = guarded_getaddrinfo  # type: ignore[assignment]
    for resolver_name in (
        "gethostbyname",
        "gethostbyname_ex",
        "gethostbyaddr",
        "getnameinfo",
    ):
        setattr(socket, resolver_name, guarded_name_resolution)
    socket.socket.bind = guarded_bind  # type: ignore[method-assign]


def _bind_listener() -> socket.socket:
    """Bind one operating-system-selected TCP port on IPv4 loopback only."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((_FIXTURE_HOST, 0))
    listener.listen(64)
    listener.setblocking(False)
    return listener


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build JSON objects while rejecting duplicate property names."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    """Reject non-standard JSON constants such as NaN and Infinity."""
    raise ValueError("non-standard JSON number")


def _read_bounded_regular_file(path: Path, maximum: int) -> bytes:
    """Read one absolute regular local file without following a leaf symlink."""
    if not path.is_absolute():
        raise ValueError("captured input path must be absolute")
    try:
        info = path.lstat()
    except OSError as exc:
        raise ValueError("captured input is unavailable") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
        raise ValueError("captured input is not a bounded regular file")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        with os.fdopen(descriptor, "rb") as source:
            content = source.read(maximum + 1)
    except OSError as exc:
        raise ValueError("captured input is unavailable") from exc
    if len(content) > maximum:
        raise ValueError("captured input exceeds its byte limit")
    return content


def _validate_sha256(value: object) -> str:
    """Require one lowercase hexadecimal SHA-256 digest."""
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError("captured body digest is invalid")
    return value


def _parse_observed_at(value: object) -> datetime:
    """Parse one explicit timezone-aware capture observation time."""
    if not isinstance(value, str):
        raise ValueError("capture observation time is invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as exc:
        raise ValueError("capture observation time is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("capture observation time must be aware")
    return parsed.astimezone(timezone.utc)


def _require_object(value: object) -> dict[str, object]:
    """Require one JSON object without coercion."""
    if not isinstance(value, dict):
        raise ValueError("capture metadata shape is invalid")
    return value


def _require_text(mapping: dict[str, object], key: str) -> str:
    """Require one bounded non-empty string field."""
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError("capture metadata field is invalid")
    return value


def _verify_capture_metadata(
    metadata_path: Path,
    *,
    raw_body: bytes,
    raw_body_sha256: str,
    observed_at: datetime,
    raw_file_name: str,
) -> None:
    """Fail closed unless the capture metadata and body describe this sample exactly."""
    raw = _read_bounded_regular_file(metadata_path, _MAX_CAPTURE_METADATA_BYTES)
    try:
        document = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("capture metadata is not strict JSON") from exc
    metadata = _require_object(document)
    request = _require_object(metadata.get("request"))

    if (
        type(metadata.get("schema_version")) is not int
        or metadata["schema_version"] != 1
        or metadata.get("source") != "akshare"
        or type(metadata.get("http_status")) is not int
        or metadata["http_status"] != 200
        or metadata.get("response_body_saved") is not True
        or metadata.get("outcome") != "bounded_sample_captured"
        or metadata.get("completeness_assessment") != "not_evaluated"
        or _require_text(metadata, "raw_sha256") != raw_body_sha256
        or type(metadata.get("raw_bytes")) is not int
        or metadata["raw_bytes"] != len(raw_body)
        or _require_text(metadata, "raw_file") != raw_file_name
        or type(metadata.get("row_count")) is not int
        or metadata["row_count"] != _FIXTURE_ROW_COUNT
        or _require_text(metadata, "observed_date_min") != _FIXTURE_START.isoformat()
        or _require_text(metadata, "observed_date_max") != _FIXTURE_END.isoformat()
        or _parse_observed_at(metadata.get("retrieved_at_utc")) != observed_at
        or urlsplit(_require_text(metadata, "endpoint")).hostname != "push2his.eastmoney.com"
        or request.get("method") != "GET"
        or request.get("symbol") != _FIXTURE_PROVIDER_SYMBOL
        or request.get("provider_symbol") != _FIXTURE_SYMBOL
        or request.get("start_date") != _FIXTURE_START.isoformat()
        or request.get("end_date") != _FIXTURE_END.isoformat()
        or request.get("adjustment_basis") != "unadjusted"
        or request.get("fqt") != "0"
        or request.get("klt") != "101"
        or request.get("secid") != "1.600519"
        or type(request.get("max_requests")) is not int
        or request["max_requests"] != 1
    ):
        raise ValueError("capture metadata does not match the expected replay sample")
    _verify_captured_body(raw_body, raw_body_sha256)


def _verify_captured_body(raw_body: bytes, raw_body_sha256: str) -> None:
    """Verify the captured Eastmoney body contains exactly the expected bars."""
    if hashlib.sha256(raw_body).hexdigest() != raw_body_sha256:
        raise ValueError("captured body does not match its declared digest")
    try:
        document = json.loads(
            raw_body.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("captured body is not strict JSON") from exc
    payload = _require_object(document)
    data = _require_object(payload.get("data"))
    klines = data.get("klines")
    if (
        payload.get("rc") != 0
        or data.get("code") != _FIXTURE_SYMBOL
        or data.get("market") != 1
        or not isinstance(klines, list)
        or len(klines) != _FIXTURE_ROW_COUNT
    ):
        raise ValueError("captured body identity or row count is invalid")
    observed_dates: list[date] = []
    for line in klines:
        if not isinstance(line, str):
            raise ValueError("captured body row is invalid")
        token = line.split(",", 1)[0]
        try:
            parsed = date.fromisoformat(token)
        except ValueError as exc:
            raise ValueError("captured body row date is invalid") from exc
        if parsed.isoformat() != token:
            raise ValueError("captured body row date is not canonical")
        observed_dates.append(parsed)
    if tuple(observed_dates) != _FIXTURE_DATES:
        raise ValueError("captured body dates do not match the captured window")


def _instrument_reference_registry(contract: Any) -> Any:
    """Build one synthetic lifecycle and alias covering exactly the captured window."""
    from opendata.data.instrument_reference import (
        InstrumentAlias,
        InstrumentLifecycle,
        InstrumentReferenceRegistry,
        LifecycleStatus,
    )

    return InstrumentReferenceRegistry(
        registry_version="stock-daily-real-sample-replay-v1",
        evidence_ref="tests/test_stock_daily_real_sample_replay_fixture_server.py",
        lifecycles=(
            InstrumentLifecycle(
                dataset_id=contract.dataset_id,
                source_id=contract.source.source_id,
                instrument_id="CN:600519",
                lifecycle_version="captured-window-life-v1",
                status=LifecycleStatus.ACTIVE,
                status_as_of=_FIXTURE_END,
                status_evidence_ref="fixture:captured-window-status",
                known_start_date=_FIXTURE_START,
                start_boundary_unknown=False,
                start_evidence_ref="fixture:captured-window-start",
            ),
        ),
        aliases=(
            InstrumentAlias(
                dataset_id=contract.dataset_id,
                source_id=contract.source.source_id,
                alias=_FIXTURE_PROVIDER_SYMBOL,
                instrument_id="CN:600519",
                alias_version="captured-window-ticker-v1",
                evidence_ref="fixture:captured-window-alias",
                valid_from=_FIXTURE_START,
                valid_through=_FIXTURE_END,
            ),
        ),
    )


def _run_backfill(
    engine: Any,
    *,
    contract: Any,
    task: Any,
    raw_body: bytes,
    observed_at: datetime,
    state_directory: Path,
) -> Any:
    """Execute the reviewed offline backfill into the disposable SQLite database."""
    from opendata.data.dataset_contract_registry import DatasetContractRegistry
    from opendata.data.instrument_reference_snapshot import (
        encode_instrument_reference_snapshot,
    )
    from opendata.pipeline.raw_evidence import RawResponseArtifactWriter
    from opendata.pipeline.reviewed_snapshot_backfill import (
        build_stock_daily_backfill_plan_from_snapshot,
    )
    from opendata.pipeline.run_dispatch import ReviewedTaskCatalog, RunDispatcher
    from opendata.pipeline.run_ledger import RunLedger
    from opendata.pipeline.run_service import ReviewedJobRegistry, RunService
    from opendata.pipeline.stock_daily_backfill_executor import (
        EXECUTION_AUTHORIZATION,
        StockDailyBackfillExecutor,
    )
    from opendata.pipeline.stock_daily_job import (
        StockDailyJsonResponse,
        build_stock_daily_job,
    )

    contract_registry = DatasetContractRegistry([contract])

    def response_source(_query: object) -> Any:
        """Return the exact captured body with the recorded observation time."""
        return StockDailyJsonResponse(
            body=raw_body,
            status_code=200,
            observed_at=observed_at,
        )

    unreviewed = build_stock_daily_job(
        task,
        schema_version=contract.schema_version,
        semantic_version=contract.semantic_version,
        contract_registry=contract_registry,
        artifact_writer=RawResponseArtifactWriter(state_directory / "private-raw"),
        response_source=response_source,
    )
    job = replace(unreviewed, reviewed=True, execution_only=True)
    registry = ReviewedJobRegistry([job])
    ledger = RunLedger(engine)
    service = RunService(ledger, registry)

    class _Worker:
        """Adapt the run service to the executor's single-run protocol."""

        def run_once(self) -> Any:
            """Claim and execute at most one queued run."""
            return service.run_once(
                "stock-daily-real-sample-replay",
                timedelta(seconds=30),
            )

    reference_registry = _instrument_reference_registry(contract)
    snapshot_bytes = encode_instrument_reference_snapshot(reference_registry)
    candidate = build_stock_daily_backfill_plan_from_snapshot(
        snapshot_bytes,
        task=task,
        contract=contract,
        as_of=_FIXTURE_END,
    )
    chunks = candidate.plan.candidate_chunks
    if len(chunks) != 1:
        raise ValueError("captured sample must yield exactly one candidate chunk")
    chunk = chunks[0]
    if (
        chunk.window_start != _FIXTURE_START
        or chunk.window_end != _FIXTURE_END
        or chunk.provider_symbol != _FIXTURE_SYMBOL
    ):
        raise ValueError("candidate chunk window does not match the captured body")

    executor = StockDailyBackfillExecutor(
        task=task,
        registry=registry,
        dispatcher=RunDispatcher(ledger, ReviewedTaskCatalog([job])),
        ledger=ledger,
        worker=_Worker(),
        max_chunks_per_invocation=1,
        max_failures=1,
        dry_run=False,
        execution_authorization=EXECUTION_AUTHORIZATION,
    )
    receipt = executor.execute_once(
        candidate.plan,
        snapshot_sha256=candidate.snapshot_bytes_sha256,
    )
    if (
        receipt.stop_reason != "plan_finished"
        or receipt.chunks_executed != 1
        or receipt.chunks_failed != 0
        or receipt.chunks_skipped != 0
    ):
        raise ValueError("reviewed backfill did not execute exactly one chunk")
    return receipt


def _is_sha256(value: object) -> bool:
    """Return whether a value is a lowercase hexadecimal SHA-256 digest."""
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _verify_publication(store: Any, *, raw_body_sha256: str) -> Any:
    """Require the exact captured row set and provenance at one publication."""
    publications = store.list_publications(_FIXTURE_DATASET_ID)
    if len(publications) != 1:
        raise ValueError("replay must produce exactly one publication")
    publication = publications[0]
    if (
        publication.dataset_id != _FIXTURE_DATASET_ID
        or publication.source_id != _FIXTURE_SOURCE_ID
        or publication.semantic_version != _FIXTURE_SEMANTIC_VERSION
        or publication.unit != "per-field"
        or publication.adjustment_basis != "raw"
        or publication.record_count != _FIXTURE_ROW_COUNT
        or not _is_sha256(publication.content_sha256)
        or publication.quality.get("status") != "warning"
        or not _is_sha256(publication.provenance.get("contract_sha256"))
        or publication.provenance.get("source_response_sha256") != raw_body_sha256
        or publication.provenance.get("source_verification") != "not_performed"
    ):
        raise ValueError("replay publication identity does not match the captured sample")
    facts = store.read_coverage_records(_FIXTURE_DATASET_ID, publication.publication_id)
    if facts is None or len(facts) != _FIXTURE_ROW_COUNT:
        raise ValueError("replay publication row count does not match the captured sample")
    for fact, expected_date in zip(facts, _FIXTURE_DATES, strict=True):
        payload = fact.payload
        if (
            fact.dataset_id != _FIXTURE_DATASET_ID
            or fact.publication_id != publication.publication_id
            or fact.business_date != expected_date
            or payload.get("market") != _FIXTURE_SOURCE_MARKET
            or payload.get("symbol") != _FIXTURE_SYMBOL
            or payload.get("trade_date") != expected_date.isoformat()
        ):
            raise ValueError("replay publication row does not match the captured sample")
    return publication


def _build_runtime(
    state_directory: Path,
    *,
    raw_body: bytes,
    raw_body_sha256: str,
    observed_at: datetime,
) -> _FixtureRuntime:
    """Replay one captured body, verify its publication, and mount the real route."""
    repository_root = Path(__file__).resolve().parents[1]
    if str(repository_root) not in sys.path:
        sys.path.insert(0, str(repository_root))

    from fastapi import FastAPI, Request
    from sqlalchemy import create_engine
    from starlette.responses import JSONResponse

    from opendata.api.dependencies import Principal, get_current_principal
    from opendata.api.published_access import (
        DatasetGrant,
        DatasetGrantRegistry,
        require_published_access,
    )
    from opendata.api.published_data import (
        get_dataset_contracts,
        get_dataset_grants,
        get_publication_store,
    )
    from opendata.api.stock_daily_capacity import (
        StockDailyCapacityAttestation,
        get_stock_daily_capacity_gate,
    )
    from opendata.api.stock_daily_publication import DATASET_ID, router
    from opendata.data.dataset_contract_registry import (
        DatasetContractRegistry,
        canonical_sha256,
    )
    from opendata.models.user import User, UserRole
    from opendata.pipeline.publication_store import (
        PUBLICATION_METADATA,
        PublicationStore,
    )
    from opendata.pipeline.run_ledger import RUN_LEDGER_METADATA
    from opendata.pipeline.stock_daily_backfill_executor import (
        build_stock_daily_execution_contract,
        build_stock_daily_execution_task,
    )

    publication_path = state_directory / "publication.sqlite"
    engine = create_engine(f"sqlite:///{publication_path}")
    try:
        RUN_LEDGER_METADATA.create_all(engine)
        PUBLICATION_METADATA.create_all(engine)
        contract = build_stock_daily_execution_contract()
        task = build_stock_daily_execution_task()
        _run_backfill(
            engine,
            contract=contract,
            task=task,
            raw_body=raw_body,
            observed_at=observed_at,
            state_directory=state_directory,
        )
        store = PublicationStore(
            engine,
            cursor_signing_key=secrets.token_bytes(32),
        )
        publication = _verify_publication(store, raw_body_sha256=raw_body_sha256)
    except Exception:
        engine.dispose()
        raise
    publication_path.chmod(0o600)

    grant = DatasetGrant(
        dataset_id=DATASET_ID,
        domain="equity",
        source_id=_FIXTURE_SOURCE_ID,
        permitted_operations=("data.read", "snapshot.create"),
        purposes=("research",),
        valid_from=datetime(2020, 1, 1, tzinfo=timezone.utc),
        valid_until=datetime(2100, 1, 1, tzinfo=timezone.utc),
        review_evidence="TST-204-STOCK-DAILY-REAL-SAMPLE-REPLAY",
    )
    user = User(
        id=204,
        username="stock-daily-real-sample-replay-user",
        email="stock-daily-real-sample-replay-user@example.test",
        hashed_password="fixture-only-unused",
        role=UserRole.USER,
        is_active=True,
    )
    principal = Principal(user=user)
    grants = DatasetGrantRegistry((grant,))
    contracts = DatasetContractRegistry((contract,))
    attestation = StockDailyCapacityAttestation(
        schema_version="stock-daily-capacity-v1",
        dataset_id=DATASET_ID,
        source_id=_FIXTURE_SOURCE_ID,
        semantic_version=contract.semantic_version,
        contract_sha256=canonical_sha256(contract),
        publication_id=publication.publication_id,
        max_window_days=3_660,
        max_page_size=_FIXTURE_PAGE_SIZE,
        not_before=datetime(2020, 1, 1, tzinfo=timezone.utc),
        expires_at=datetime(2100, 1, 1, tzinfo=timezone.utc),
        evidence_ref="tests/test_stock_daily_real_sample_replay_fixture_server.py",
        attested_by="captured-real-sample-offline-replay",
    )
    read_access = require_published_access(principal, DATASET_ID, "data.read", grants)
    expected_snapshot = store.create_snapshot(
        DATASET_ID,
        publication_id=publication.publication_id,
        start_date=_FIXTURE_START,
        end_date=_FIXTURE_END_EXCLUSIVE,
        page_size=_FIXTURE_PAGE_SIZE,
        access_scope=read_access.binding_sha256,
        market=_FIXTURE_SOURCE_MARKET,
        symbol=_FIXTURE_SYMBOL,
    )
    if (
        expected_snapshot.record_count != _FIXTURE_ROW_COUNT
        or expected_snapshot.start_date != _FIXTURE_START
        or expected_snapshot.end_date != _FIXTURE_END_EXCLUSIVE
    ):
        raise ValueError("scoped publication snapshot does not match the captured sample")

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(router)
    app.dependency_overrides[get_current_principal] = lambda: principal
    app.dependency_overrides[get_dataset_grants] = lambda: grants
    app.dependency_overrides[get_dataset_contracts] = lambda: contracts
    app.dependency_overrides[get_publication_store] = lambda: store
    app.dependency_overrides[get_stock_daily_capacity_gate] = lambda: attestation

    audit_rows: list[dict[str, str]] = []
    audit_lock = threading.Lock()

    @app.middleware("http")
    async def audit_v2_requests(request: Request, call_next: Any) -> Any:
        """Record only the method and path of v2 calls, never request metadata."""
        if request.scope.get("path", "").startswith("/api/v2/"):
            with audit_lock:
                audit_rows.append({"method": request.method, "path": request.scope["path"]})
        return await call_next(request)

    @app.get("/_fixture/health", include_in_schema=False)
    async def fixture_health() -> JSONResponse:
        """Expose a constant local readiness response without credentials."""
        return JSONResponse({"status": "ready"})

    @app.get("/_fixture/audit", include_in_schema=False)
    async def fixture_audit() -> JSONResponse:
        """Return the method/path audit trail without query or auth data."""
        with audit_lock:
            response_rows = [dict(row) for row in audit_rows]
        return JSONResponse({"requests": response_rows})

    return _FixtureRuntime(
        app=app,
        engine=engine,
        publication_id=publication.publication_id,
        content_sha256=expected_snapshot.content_sha256,
        record_count=expected_snapshot.record_count,
        api_key=f"synthetic-afi204-{secrets.token_hex(16)}",
    )


def _write_ready_file(path: Path, runtime: _FixtureRuntime, port: int) -> None:
    """Atomically publish endpoint metadata with owner-only file permissions."""
    payload = {
        "base_url": f"http://{_FIXTURE_HOST}:{port}",
        "api_key": runtime.api_key,
        "dataset_id": _FIXTURE_DATASET_ID,
        "publication_id": runtime.publication_id,
        "content_sha256": runtime.content_sha256,
        "record_count": runtime.record_count,
        "start_date": _FIXTURE_START.isoformat(),
        "end_date": _FIXTURE_END_EXCLUSIVE.isoformat(),
    }
    temporary_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(
        temporary_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as ready_file:
            json.dump(payload, ready_file, separators=(",", ":"))
            ready_file.write("\n")
            ready_file.flush()
            os.fsync(ready_file.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def _read_shutdown_command(stop_event: threading.Event) -> None:
    """Stop on the explicit command or on stdin closure from the test harness."""
    try:
        for line in sys.stdin:
            if line.strip() == "shutdown":
                break
    finally:
        stop_event.set()


def _stop_server(server: Any, thread: threading.Thread, listener: socket.socket) -> bool:
    """Request graceful exit, then force-close the exact listener after a bound."""
    server.should_exit = True
    thread.join(_SHUTDOWN_TIMEOUT_SECONDS)
    if thread.is_alive():
        server.force_exit = True
        listener.close()
        thread.join(2.0)
    return not thread.is_alive()


def _run_server(runtime: _FixtureRuntime, listener: socket.socket, ready_path: Path) -> int:
    """Serve the app until a private shutdown signal arrives, then dispose pools."""
    import uvicorn

    port = listener.getsockname()[1]
    stop_event = threading.Event()
    thread_errors: list[bool] = []
    server = uvicorn.Server(
        uvicorn.Config(
            runtime.app,
            host=_FIXTURE_HOST,
            port=port,
            log_level="critical",
            access_log=False,
            log_config=None,
            lifespan="off",
            timeout_graceful_shutdown=2,
        )
    )

    def serve_thread() -> None:
        try:
            asyncio.run(server.serve(sockets=[listener]))
        except BaseException:
            thread_errors.append(True)

    server_thread = threading.Thread(
        target=serve_thread,
        name="opendata-stock-daily-replay-fixture",
        daemon=False,
    )
    server_thread.start()
    deadline = time.monotonic() + _STARTUP_TIMEOUT_SECONDS
    while not server.started and server_thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started or thread_errors:
        _stop_server(server, server_thread, listener)
        return 1

    try:
        _write_ready_file(ready_path, runtime, port)
    except Exception:
        _stop_server(server, server_thread, listener)
        return 1

    stop_thread = threading.Thread(
        target=_read_shutdown_command,
        args=(stop_event,),
        name="opendata-stock-daily-replay-fixture-shutdown",
        daemon=True,
    )
    stop_thread.start()
    original_sigterm = signal.getsignal(signal.SIGTERM)
    original_sigint = signal.getsignal(signal.SIGINT)

    def request_shutdown(signum: int, frame: Any) -> None:
        """Convert process termination into graceful Uvicorn shutdown."""
        del signum, frame
        stop_event.set()

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    try:
        while not stop_event.wait(0.05) and server_thread.is_alive():
            pass
        _stop_server(server, server_thread, listener)
    finally:
        signal.signal(signal.SIGTERM, original_sigterm)
        signal.signal(signal.SIGINT, original_sigint)
    return 1 if thread_errors or server_thread.is_alive() else 0


def main(argv: Sequence[str] | None = None) -> int:
    """Replay one verified captured body and serve only the loopback stock route."""
    args = _parse_args(argv)
    ready_path: Path | None = None
    runtime: _FixtureRuntime | None = None
    try:
        ready_path = _validate_ready_path(args.ready_file)
        raw_body = _read_bounded_regular_file(args.raw_body, _MAX_SAMPLE_BYTES)
        expected_sha256 = _validate_sha256(args.raw_body_sha256)
        actual_sha256 = hashlib.sha256(raw_body).hexdigest()
        if not hmac.compare_digest(actual_sha256, expected_sha256):
            raise ValueError("captured body does not match the requested digest")
        observed_at = _parse_observed_at(args.observed_at)
        _verify_capture_metadata(
            args.capture_metadata,
            raw_body=raw_body,
            raw_body_sha256=actual_sha256,
            observed_at=observed_at,
            raw_file_name=args.raw_body.name,
        )
        with tempfile.TemporaryDirectory(
            prefix="opendata-stock-daily-real-sample-replay-",
            dir=ready_path.parent,
        ) as temporary_root:
            state_directory = Path(temporary_root)
            state_directory.chmod(0o700)
            _configure_isolated_environment(state_directory)
            listener = _bind_listener()
            _install_loopback_egress_guard(listener)
            runtime = _build_runtime(
                state_directory,
                raw_body=raw_body,
                raw_body_sha256=actual_sha256,
                observed_at=observed_at,
            )
            try:
                return _run_server(runtime, listener, ready_path)
            finally:
                listener.close()
                runtime.engine.dispose()
                ready_path.unlink(missing_ok=True)
    except Exception:
        if runtime is not None:
            runtime.engine.dispose()
        if ready_path is not None:
            ready_path.unlink(missing_ok=True)
        sys.stderr.write("stock-daily real-sample replay fixture server failed\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
