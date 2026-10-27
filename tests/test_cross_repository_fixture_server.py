"""Private loopback fixture server for cross-repository REST integration tests.

Run this file as a subprocess with ``--ready-file`` inside a private temporary
directory. It mounts the real OpenData v2 router, seeds synthetic records into
an explicitly owned SQLite publication database, and authenticates a synthetic
API key against a separate SQLite auth database. It never starts the full app
or its scheduler and never contacts a provider.
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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

_FIXTURE_HOST = "127.0.0.1"
_FIXTURE_DATASET_ID = "cn.csi300.index.daily"
_FIXTURE_SYMBOL = "000300.SH"
_FIXTURE_SOURCE_ID = "ths.index_daily"
_FIXTURE_START = date(2024, 1, 1)
_FIXTURE_END_EXCLUSIVE = date(2024, 1, 5)
_FIXTURE_ROWS: tuple[tuple[date, float], ...] = (
    (date(2024, 1, 2), 1000.0),
    (date(2024, 1, 3), 1010.0),
    (date(2024, 1, 4), 990.0),
)
_STARTUP_TIMEOUT_SECONDS = 20.0
_SHUTDOWN_TIMEOUT_SECONDS = 8.0


@dataclass(slots=True)
class _FixtureRuntime:
    """Hold all resources owned by one synthetic REST fixture process."""

    app: Any
    auth_engine: Any
    publication_engine: Any
    publication_id: str
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

    auth_database = state_directory / "auth.sqlite"
    publication_database = state_directory / "publication.sqlite"
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
            "DATABASE_URL": f"sqlite+aiosqlite:///{auth_database}",
            "DATA_DATABASE_URL": f"sqlite:///{publication_database}",
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


def _build_runtime(state_directory: Path) -> _FixtureRuntime:
    """Seed isolated auth/publication databases and mount the real v2 router."""
    repository_root = Path(__file__).resolve().parents[1]
    if str(repository_root) not in sys.path:
        sys.path.insert(0, str(repository_root))

    from fastapi import FastAPI, Request
    from sqlalchemy import create_engine
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.orm import Session
    from starlette.responses import JSONResponse

    from opendata.api.published_access import DatasetGrant, DatasetGrantRegistry
    from opendata.api.published_data import (
        get_dataset_contracts,
        get_dataset_coverage_evidence_registry_loader,
        get_dataset_grants,
        get_publication_store,
        router,
    )
    from opendata.core.database import Base, get_db
    from opendata.data.coverage_evidence_registry import (
        DatasetCoverageEvidenceRegistry,
    )
    from opendata.data.dataset_contract_registry import (
        DatasetContractRegistry,
        canonical_sha256,
    )
    from opendata.data.providers.ths.index_daily_publication import (
        INDEX_SOURCE_ID,
        build_index_daily_contract,
    )
    from opendata.models.api_key import ApiKey, ApiKeyStatus
    from opendata.models.user import User, UserRole
    from opendata.pipeline.publication_store import (
        PUBLICATION_METADATA,
        PublicationStore,
        PublishedRecordInput,
    )
    from opendata.services.api_key_service import generate_key, hash_key

    auth_path = state_directory / "auth.sqlite"
    publication_path = state_directory / "publication.sqlite"
    auth_sync_engine = create_engine(f"sqlite:///{auth_path}")
    publication_engine = create_engine(f"sqlite:///{publication_path}")
    try:
        Base.metadata.create_all(auth_sync_engine, tables=[User.__table__, ApiKey.__table__])
        PUBLICATION_METADATA.create_all(publication_engine)
        api_key = generate_key()
        fixture_valid_until = datetime.now(timezone.utc) + timedelta(days=3650)
        scopes = [
            "index",
            "dataset.read",
            "data.read",
            "coverage.read",
            "snapshot.create",
            "snapshot.read",
        ]
        with Session(auth_sync_engine) as session:
            owner = User(
                username="cross-repo-fixture",
                email="cross-repo-fixture@example.test",
                hashed_password="fixture-only-unused",
                role=UserRole.USER,
                is_active=True,
            )
            session.add(owner)
            session.flush()
            session.add(
                ApiKey(
                    key_hash=hash_key(api_key),
                    name="cross-repository synthetic fixture",
                    owner_user_id=owner.id,
                    scopes=scopes,
                    rate_limit=1000,
                    status=ApiKeyStatus.ACTIVE,
                    expires_at=fixture_valid_until,
                )
            )
            session.commit()
    finally:
        auth_sync_engine.dispose()
    auth_path.chmod(0o600)

    auth_async_engine = create_async_engine(f"sqlite+aiosqlite:///{auth_path}")
    auth_session_factory = async_sessionmaker(auth_async_engine, expire_on_commit=False)
    store = PublicationStore(
        publication_engine,
        cursor_signing_key=secrets.token_bytes(32),
    )
    contract = build_index_daily_contract(_FIXTURE_SYMBOL)
    records = tuple(
        PublishedRecordInput(
            business_key=json.dumps(
                (_FIXTURE_SYMBOL, day.isoformat()),
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            business_date=day,
            payload={
                "symbol": _FIXTURE_SYMBOL,
                "trade_date": day.isoformat(),
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": 10.0,
            },
        )
        for day, close in _FIXTURE_ROWS
    )
    publication = store.publish(
        dataset_id=_FIXTURE_DATASET_ID,
        source_id=INDEX_SOURCE_ID,
        schema_version=contract.schema_version,
        semantic_version=contract.semantic_version,
        unit="native",
        adjustment_basis=contract.adjustment_basis,
        quality={"status": "passed", "report_id": "SYNTHETIC-CROSSREPO", "issue_count": 0},
        provenance={
            "contract_sha256": canonical_sha256(contract),
            "fixture": "synthetic-cross-repository-v1",
        },
        idempotency_key=secrets.token_hex(16),
        records=records,
    )
    publication_path.chmod(0o600)
    contract_hash = canonical_sha256(contract)
    visible_records = store.read_coverage_record_set(
        _FIXTURE_DATASET_ID,
        publication.publication_id,
        contract_sha256=contract_hash,
    )
    if visible_records is None or visible_records.record_count != len(records):
        raise RuntimeError("synthetic coverage record-set pin could not be created")
    evidence = _build_coverage_evidence(
        publication.publication_id,
        dataset_id=_FIXTURE_DATASET_ID,
        source_id=INDEX_SOURCE_ID,
        symbol=_FIXTURE_SYMBOL,
        visible_records=visible_records,
    )
    grants = DatasetGrantRegistry(
        (
            DatasetGrant(
                dataset_id=_FIXTURE_DATASET_ID,
                domain="index",
                source_id=INDEX_SOURCE_ID,
                permitted_operations=(
                    "dataset.read",
                    "data.read",
                    "coverage.read",
                    "snapshot.create",
                    "snapshot.read",
                ),
                purposes=("research",),
                valid_from=datetime(2020, 1, 1, tzinfo=timezone.utc),
                valid_until=fixture_valid_until,
                review_evidence="TST-204-CROSSREPO",
            ),
        )
    )
    contracts = DatasetContractRegistry((contract,))
    evidence_registry = DatasetCoverageEvidenceRegistry((evidence,))

    async def fixture_get_db() -> AsyncIterator[Any]:
        """Yield one session from the explicit temporary auth database."""
        async with auth_session_factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(router)
    app.dependency_overrides[get_db] = fixture_get_db
    app.dependency_overrides[get_dataset_grants] = lambda: grants
    app.dependency_overrides[get_dataset_contracts] = lambda: contracts
    app.dependency_overrides[get_publication_store] = lambda: store
    app.dependency_overrides[get_dataset_coverage_evidence_registry_loader] = (
        lambda: lambda: evidence_registry
    )

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
        auth_engine=auth_async_engine,
        publication_engine=publication_engine,
        publication_id=publication.publication_id,
        api_key=api_key,
    )


def _build_coverage_evidence(
    publication_id: str,
    *,
    dataset_id: str,
    source_id: str,
    symbol: str,
    visible_records: Any,
) -> Any:
    """Bind synthetic lifecycle, open dates, and publication scan to one pin."""
    from opendata.data.coverage_evidence_registry import (
        DatasetCoverageEvidence,
        VisibleRecordSetPin,
    )
    from opendata.data.instrument_reference import (
        InstrumentLifecycle,
        InstrumentReferenceRegistry,
    )
    from opendata.pipeline.coverage_service import CalendarEvidence

    last_day = _FIXTURE_END_EXCLUSIVE - timedelta(days=1)
    lifecycle = InstrumentLifecycle(
        dataset_id=dataset_id,
        source_id=source_id,
        instrument_id=symbol,
        lifecycle_version="synthetic-cross-repo-lifecycle-v1",
        status="active",
        status_as_of=last_day,
        status_evidence_ref="docs/evidence/coverage/cross-repo-synthetic-lifecycle.json",
        known_start_date=_FIXTURE_START,
        start_boundary_unknown=False,
        start_evidence_ref="docs/evidence/coverage/cross-repo-synthetic-start.json",
    )
    instrument_registry = InstrumentReferenceRegistry(
        registry_version="synthetic-cross-repo-instruments-v1",
        evidence_ref="docs/evidence/coverage/cross-repo-synthetic-instruments.json",
        lifecycles=(lifecycle,),
    )
    calendar = CalendarEvidence(
        dataset_id=dataset_id,
        source_id=source_id,
        instrument_id=symbol,
        calendar_id="synthetic-cross-repo-calendar",
        calendar_version="synthetic-cross-repo-calendar-v1",
        evidence_ref="docs/evidence/coverage/cross-repo-synthetic-calendar.json",
        covered_from=_FIXTURE_START,
        covered_through=last_day,
        open_dates=tuple(day for day, _ in _FIXTURE_ROWS),
        daily_classification_complete=True,
    )
    return DatasetCoverageEvidence(
        dataset_id=dataset_id,
        source_id=source_id,
        publication_id=publication_id,
        instrument_identifier=symbol,
        instrument_registry=instrument_registry,
        calendar=calendar,
        publication_scope_start=_FIXTURE_START,
        publication_scope_end_exclusive=_FIXTURE_END_EXCLUSIVE,
        publication_scope_evidence_ref=(
            "docs/evidence/coverage/cross-repo-synthetic-publication.json"
        ),
        visible_record_set=VisibleRecordSetPin(
            algorithm=visible_records.algorithm,
            sha256=visible_records.sha256,
            record_count=visible_records.record_count,
            publication_sequence=visible_records.publication_sequence,
            contract_sha256=visible_records.contract_sha256,
        ),
    )


def _write_ready_file(path: Path, runtime: _FixtureRuntime, port: int) -> None:
    """Atomically publish endpoint metadata with owner-only file permissions."""
    payload = {
        "base_url": f"http://{_FIXTURE_HOST}:{port}",
        "api_key": runtime.api_key,
        "dataset_id": _FIXTURE_DATASET_ID,
        "publication_id": runtime.publication_id,
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

    from opendata.core import database as database_module
    from opendata.core.api_key_rate_limit import api_key_rate_limiter

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

    async def serve_and_dispose() -> None:
        """Run Uvicorn and close every async engine on its owning event loop."""
        try:
            await server.serve(sockets=[listener])
        finally:
            await runtime.auth_engine.dispose()
            await database_module.engine.dispose()
            await database_module.data_engine.dispose()

    def serve_thread() -> None:
        try:
            asyncio.run(serve_and_dispose())
        except BaseException:
            thread_errors.append(True)

    server_thread = threading.Thread(
        target=serve_thread,
        name="opendata-cross-repository-fixture",
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
        name="opendata-cross-repository-fixture-shutdown",
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
        api_key_rate_limiter.reset_local_state()
    finally:
        signal.signal(signal.SIGTERM, original_sigterm)
        signal.signal(signal.SIGINT, original_sigint)
    return 1 if thread_errors or server_thread.is_alive() else 0


def main(argv: Sequence[str] | None = None) -> int:
    """Start the isolated synthetic API process using a private ready file."""
    args = _parse_args(argv)
    ready_path: Path | None = None
    try:
        ready_path = _validate_ready_path(args.ready_file)
        with tempfile.TemporaryDirectory(
            prefix="opendata-cross-repository-",
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
        if ready_path is not None:
            ready_path.unlink(missing_ok=True)
        sys.stderr.write("cross-repository fixture server failed\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
