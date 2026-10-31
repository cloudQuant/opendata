"""Private loopback fixture server for the pinned stock-daily v2 wire test.

Run this file as a subprocess with ``--ready-file`` inside a private temporary
directory. It mounts only the real OpenData stock-daily v2 router over a
disposable SQLite publication store seeded with two synthetic raw daily bars.
Dependencies are overridden with in-memory synthetic grants, contracts, a
principal, and a capacity attestation, so no auth database, production
database, provider, credential, or external network is used.
"""

from __future__ import annotations

import argparse
import asyncio
import errno
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
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

_FIXTURE_HOST = "127.0.0.1"
_FIXTURE_DATASET_ID = "cn-equity-stock-daily"
_FIXTURE_SOURCE_ID = "akshare.daily"
_FIXTURE_SEMANTIC_VERSION = "stock-daily-raw-v1"
_FIXTURE_MARKET = "CN-SSE"
_FIXTURE_SYMBOL = "600519"
_FIXTURE_START = date(2026, 10, 26)
_FIXTURE_END_EXCLUSIVE = date(2026, 10, 29)
_FIXTURE_ROWS: tuple[tuple[date, float], ...] = (
    (date(2026, 10, 26), 100.0),
    (date(2026, 10, 27), 101.0),
)
_FIXTURE_PAGE_SIZE = 1_000
_STARTUP_TIMEOUT_SECONDS = 20.0
_SHUTDOWN_TIMEOUT_SECONDS = 8.0


@dataclass(slots=True)
class _FixtureRuntime:
    """Hold all resources owned by one synthetic stock-daily fixture process."""

    app: Any
    publication_engine: Any
    publication_id: str
    content_sha256: str
    record_count: int
    api_key: str = field(repr=False)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Parse the single non-secret startup argument."""
    parser = argparse.ArgumentParser(add_help=True)
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


def _contract() -> Any:
    """Return the exact reviewed raw stock-daily contract."""
    from opendata.data.platform_contracts import DatasetContract, FieldContract, SourceIdentity

    return DatasetContract(
        dataset_id=_FIXTURE_DATASET_ID,
        schema_version="1.0.0",
        semantic_version=_FIXTURE_SEMANTIC_VERSION,
        asset_class="equity",
        frequency="1d",
        record_kind="time_series",
        fields=(
            FieldContract(name="market", data_type="string"),
            FieldContract(name="symbol", data_type="string"),
            FieldContract(name="trade_date", data_type="date"),
            FieldContract(name="open", data_type="number"),
            FieldContract(name="high", data_type="number"),
            FieldContract(name="low", data_type="number"),
            FieldContract(name="close", data_type="number"),
            FieldContract(name="volume", data_type="number"),
            FieldContract(name="amount", data_type="number"),
        ),
        business_key=("market", "symbol", "trade_date"),
        ordering=("trade_date", "market", "symbol"),
        filter_fields=("market", "symbol"),
        time_field="trade_date",
        temporal_semantics="business_date",
        source=SourceIdentity(source_id=_FIXTURE_SOURCE_ID, source="akshare"),
        field_units={
            "open": "CNY/share",
            "high": "CNY/share",
            "low": "CNY/share",
            "close": "CNY/share",
            "volume": "share",
            "amount": "CNY",
        },
        field_currencies={
            "open": "CNY",
            "high": "CNY",
            "low": "CNY",
            "close": "CNY",
            "amount": "CNY",
        },
        currency="CNY",
        adjustment_basis="raw",
        field_adjustments={
            "open": "raw",
            "high": "raw",
            "low": "raw",
            "close": "raw",
            "volume": "not_applicable",
            "amount": "not_applicable",
        },
        calendar_id="SSE",
        timezone="Asia/Shanghai",
        pit_policy="unsupported",
    )


def _record(trade_date: date, close: float) -> Any:
    """Create one normalized synthetic raw daily-bar record."""
    from opendata.pipeline.publication_store import PublishedRecordInput

    market = "cn"
    business_key = json.dumps(
        (market, _FIXTURE_SYMBOL, trade_date.isoformat()),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    return PublishedRecordInput(
        business_key=business_key,
        payload={
            "market": market,
            "symbol": _FIXTURE_SYMBOL,
            "trade_date": trade_date.isoformat(),
            "open": close - 1,
            "high": close + 1,
            "low": close - 2,
            "close": close,
            "volume": 1000.0,
            "amount": 120000.0,
        },
        business_date=trade_date,
        market=market,
        symbol=_FIXTURE_SYMBOL,
    )


def _build_runtime(state_directory: Path) -> _FixtureRuntime:
    """Seed one synthetic publication and mount the real stock-daily router."""
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

    publication_path = state_directory / "publication.sqlite"
    publication_engine = create_engine(f"sqlite:///{publication_path}")
    try:
        PUBLICATION_METADATA.create_all(publication_engine)
        store = PublicationStore(
            publication_engine,
            cursor_signing_key=secrets.token_bytes(32),
        )
        contract = _contract()
        publication = store.publish(
            dataset_id=DATASET_ID,
            source_id=_FIXTURE_SOURCE_ID,
            schema_version=contract.schema_version,
            semantic_version=contract.semantic_version,
            unit="per-field",
            adjustment_basis="raw",
            quality={
                "status": "warning",
                "report_id": "stock-daily-synthetic-cross-repository-v1",
                "issue_count": 0,
            },
            provenance={
                "contract_sha256": canonical_sha256(contract),
                "fixture": "synthetic-stock-daily-cross-repository-v1",
                "source_verification": "not_performed",
                "source_history_limit": "unknown",
            },
            idempotency_key=secrets.token_hex(16),
            records=tuple(_record(day, close) for day, close in _FIXTURE_ROWS),
        )
    except Exception:
        publication_engine.dispose()
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
        review_evidence="TST-204-STOCK-DAILY-CROSSREPO",
    )
    user = User(
        id=204,
        username="stock-daily-cross-repository-user",
        email="stock-daily-cross-repository-user@example.test",
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
        evidence_ref="tests/test_stock_daily_cross_repository_fixture_server.py",
        attested_by="synthetic-cross-repository-fixture",
    )
    read_access = require_published_access(principal, DATASET_ID, "data.read", grants)
    expected_snapshot = store.create_snapshot(
        DATASET_ID,
        publication_id=publication.publication_id,
        start_date=_FIXTURE_START,
        end_date=_FIXTURE_END_EXCLUSIVE,
        page_size=_FIXTURE_PAGE_SIZE,
        access_scope=read_access.binding_sha256,
        market="cn",
        symbol=_FIXTURE_SYMBOL,
    )

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
        publication_engine=publication_engine,
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
        name="opendata-stock-daily-cross-repository-fixture",
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
        name="opendata-stock-daily-cross-repository-fixture-shutdown",
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
    """Start the isolated synthetic stock-daily API using a private ready file."""
    args = _parse_args(argv)
    ready_path: Path | None = None
    runtime: _FixtureRuntime | None = None
    try:
        ready_path = _validate_ready_path(args.ready_file)
        with tempfile.TemporaryDirectory(
            prefix="opendata-stock-daily-cross-repository-",
            dir=ready_path.parent,
        ) as temporary_root:
            state_directory = Path(temporary_root)
            state_directory.chmod(0o700)
            _configure_isolated_environment(state_directory)
            listener = _bind_listener()
            _install_loopback_egress_guard(listener)
            runtime = _build_runtime(state_directory)
            try:
                return _run_server(runtime, listener, ready_path)
            finally:
                listener.close()
                runtime.publication_engine.dispose()
                ready_path.unlink(missing_ok=True)
    except Exception:
        if runtime is not None:
            runtime.publication_engine.dispose()
        if ready_path is not None:
            ready_path.unlink(missing_ok=True)
        sys.stderr.write("stock-daily cross-repository fixture server failed\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
