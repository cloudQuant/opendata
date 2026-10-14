"""AC21 real-browser download-chain probe with an owned offline MySQL lease."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn, Protocol

import pandas as pd
import pytest
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import tests.script_download_backend_harness as ac21_harness
from tests.script_download_backend_harness import (
    DatabaseTargetError,
    DockerCommandError,
    OwnedMysql,
    assert_guard_counterfactuals,
    guard_database_url,
    sha256_file,
    utc_now,
    write_json,
)

if TYPE_CHECKING:
    from opendata.models.interface import DataInterface

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
EVIDENCE_ROOT = ROOT / "docs/evidence/C100/frontend-download"
PRODUCT_FILES = (
    "frontend/src/views/ScriptDetailView.vue",
    "frontend/src/views/ExecutionsView.vue",
    "frontend/src/views/LoginView.vue",
    "frontend/src/api/data.ts",
    "frontend/src/utils/request.ts",
    "frontend/src/utils/error.ts",
    "opendata/api/data.py",
    "opendata/services/data_acquisition.py",
    "opendata/models/task.py",
    "opendata/models/interface.py",
    "opendata/models/data_table.py",
)
HARNESS_FILES = (
    "tests/script_download_backend_harness.py",
    "tests/test_script_download_backend_iter2.py",
    "frontend/e2e/script-download-backend.spec.ts",
    "frontend/e2e/script-download-backend.playwright.config.ts",
    "frontend/e2e/script-download-backend-vite.config.ts",
    "frontend/src/__tests__/login-view.test.ts",
    "frontend/src/__tests__/utils/error.test.ts",
    "frontend/src/__tests__/utils/request.test.ts",
)

_HTTP_REQUEST_EVENT: ContextVar[dict[str, Any] | None] = ContextVar(
    "ac21_http_request_event", default=None
)


class _FrameCopy(Protocol):
    """Describe only the DataFrame operations used by the offline source adapter."""

    def __len__(self) -> int:
        """Return the number of fixture rows."""
        ...

    def copy(self, deep: bool = True) -> _FrameCopy:
        """Return an isolated copy of the fixture frame."""
        ...


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _file_hashes() -> dict[str, str]:
    return {path: sha256_file(ROOT / path) for path in PRODUCT_FILES}


def _harness_hashes() -> dict[str, str]:
    return {path: sha256_file(ROOT / path) for path in HARNESS_FILES}


def _runtime_module_origins() -> dict[str, dict[str, str]]:
    """Record file-backed opendata modules and their exact repository source hashes."""
    origins: dict[str, dict[str, str]] = {}
    for module_name, module in sorted(sys.modules.items()):
        if module_name != "opendata" and not module_name.startswith("opendata."):
            continue
        module_file = getattr(module, "__file__", None)
        if not isinstance(module_file, str):
            origins[module_name] = {"error": "no-file-origin"}
            continue
        module_path = Path(module_file)
        if module_path.is_symlink():
            origins[module_name] = {"error": "symlink-origin"}
            continue
        resolved_path = module_path.resolve()
        try:
            relative_path = resolved_path.relative_to(ROOT)
        except ValueError:
            origins[module_name] = {"error": "outside-repository"}
            continue
        if not resolved_path.is_file():
            origins[module_name] = {"error": "missing-file"}
            continue
        origins[module_name] = {
            "path": relative_path.as_posix(),
            "sha256": sha256_file(resolved_path),
        }
    return origins


def _redact_generated_text(value: str, generated_secrets: tuple[str, ...]) -> tuple[str, int]:
    """Remove exact run secrets from a text channel before it reaches evidence."""
    redacted = value
    replacement_count = 0
    for secret in generated_secrets:
        if secret:
            replacement_count += redacted.count(secret)
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted, replacement_count


def _redact_evidence_files(
    evidence_dir: Path, generated_secrets: tuple[str, ...]
) -> dict[str, int | bool]:
    """Scan and redact run-owned text evidence before the final manifest write."""
    scanned_files = 0
    replacements = 0
    remaining = 0
    text_suffixes = {".json", ".log", ".txt"}
    for path in sorted(evidence_dir.rglob("*")):
        if path.is_symlink() or not path.is_file() or path.suffix not in text_suffixes:
            continue
        try:
            original = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        scanned_files += 1
        redacted, found = _redact_generated_text(original, generated_secrets)
        replacements += found
        if redacted != original:
            path.write_text(redacted, encoding="utf-8")
        remaining += sum(secret in redacted for secret in generated_secrets if secret)
    return {
        "file_scan_count": scanned_files,
        "generated_value_count": sum(bool(secret) for secret in generated_secrets),
        "replacement_count": replacements,
        "remaining_match_count": remaining,
    }


def _safe_exception_record(exc: BaseException) -> dict[str, object]:
    """Return only reviewed exception identity fields, never arbitrary message text."""
    if isinstance(exc, DockerCommandError):
        return {
            "class": exc.failure_class,
            "stage": exc.stage,
            "return_code": exc.return_code,
        }
    return {"class": type(exc).__name__}


def _assert_docker_failure_redaction_counterfactual(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, object]:
    """Exercise the real harness adapter with fake Docker errors and secret output."""
    first_canary = "AC21_SYNTHETIC_SECRET_CANARY_A"
    second_canary = "AC21_SYNTHETIC_SECRET_CANARY_B"
    lease = OwnedMysql()
    lease.password = first_canary
    lease.root_password = second_canary

    def raise_process_error(arguments: list[str], **_options: object) -> NoReturn:
        raise subprocess.CalledProcessError(
            returncode=125,
            cmd=arguments,
            output=f"stdout {first_canary}",
            stderr=f"stderr {second_canary}",
        )

    with monkeypatch.context() as patcher:
        patcher.setattr(ac21_harness.shutil, "which", lambda _name: "/owned/docker/bin/docker")
        patcher.setattr(ac21_harness.subprocess, "run", raise_process_error)
        with pytest.raises(DockerCommandError) as failure_info:
            lease._docker(
                [
                    "run",
                    "--env",
                    f"MYSQL_PASSWORD={first_canary}",
                    "--env",
                    f"MYSQL_ROOT_PASSWORD={second_canary}",
                ]
            )

    safe_failure = failure_info.value
    safe_record = _safe_exception_record(safe_failure)
    serialized = json.dumps(safe_record, sort_keys=True)
    if (
        first_canary in str(safe_failure)
        or second_canary in str(safe_failure)
        or first_canary in serialized
        or second_canary in serialized
        or safe_failure.__cause__ is not None
        or safe_failure.__context__ is not None
    ):
        raise AssertionError("synthetic MySQL password escaped sanitized Docker failure")
    if safe_record != {
        "class": "CalledProcessError",
        "stage": "container-start",
        "return_code": 125,
    }:
        raise AssertionError("sanitized Docker failure lacks the reviewed class/stage/return code")

    def return_secret_output(
        arguments: list[str], **_options: object
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            arguments,
            0,
            stdout=f"stdout {first_canary}",
            stderr=f"stderr {second_canary}",
        )

    with monkeypatch.context() as patcher:
        patcher.setattr(ac21_harness.shutil, "which", lambda _name: "/owned/docker/bin/docker")
        patcher.setattr(ac21_harness.subprocess, "run", return_secret_output)
        process_result = lease._docker(["logs", "synthetic-container"], check=False)

    safe_output = "\n".join((process_result.stdout, process_result.stderr, *process_result.args))
    if first_canary in safe_output or second_canary in safe_output:
        raise AssertionError("synthetic MySQL password escaped sanitized Docker output")
    with tempfile.TemporaryDirectory(prefix="ac21-redaction-fake-") as temporary:
        evidence_dir = Path(temporary)
        fake_channels = {
            "run-manifest.json": f'{{"exception":"{first_canary}"}}',
            "pytest.stdout.log": f"stdout {first_canary}",
            "pytest.stderr.log": f"stderr {second_canary}",
            "backend-exceptions-redacted.json": f'{{"traceback":"{second_canary}"}}',
        }
        for filename, contents in fake_channels.items():
            (evidence_dir / filename).write_text(contents, encoding="utf-8")
        scan_result = _redact_evidence_files(evidence_dir, (first_canary, second_canary))
        persisted_channels = "\n".join(
            path.read_text(encoding="utf-8") for path in sorted(evidence_dir.iterdir())
        )
        if first_canary in persisted_channels or second_canary in persisted_channels:
            raise AssertionError("synthetic password remained in a harness evidence channel")
        if scan_result["remaining_match_count"] != 0:
            raise AssertionError("harness evidence scan did not prove the canary-free result")
    return {
        "failure_class_stage_return_code_checked": True,
        "subprocess_exception_context_absent": True,
        "stdout_stderr_and_args_redacted": True,
        "manifest_and_evidence_files_scanned": True,
        "synthetic_canary_values_persisted": False,
    }


def _serve(app: Any, port: int) -> tuple[Any, threading.Thread, str]:
    import uvicorn

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="critical",
            access_log=False,
            lifespan="off",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True, name="ac21-test-api")
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=5)
        raise RuntimeError("AC21 loopback API server did not start")
    return server, thread, f"http://127.0.0.1:{port}"


def _create_test_app(http_events: list[dict[str, Any]], session_maker: Any) -> Any:
    from fastapi import FastAPI

    from opendata.api.auth import router as auth_router
    from opendata.api.data import router as data_router
    from opendata.api.executions import router as executions_router
    from opendata.api.interfaces import router as interfaces_router
    from opendata.api.pipeline import router as pipeline_router
    from opendata.api.scripts import router as scripts_router
    from opendata.core.database import get_db

    app = FastAPI(title="AC21 test-owned API")
    app.include_router(auth_router, prefix="/api/v1/auth")
    app.include_router(scripts_router, prefix="/api/v1/scripts")
    app.include_router(interfaces_router, prefix="/api/v1/data/interfaces")
    app.include_router(data_router, prefix="/api/v1/data")
    app.include_router(executions_router, prefix="/api/v1/executions")
    app.include_router(pipeline_router, prefix="/api/v1")

    async def test_owned_database_session() -> Any:
        async with session_maker() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

    app.dependency_overrides[get_db] = test_owned_database_session

    @app.middleware("http")
    async def observe_requests(request: Any, call_next: Any) -> Any:
        event = {
            "method": request.method,
            "path": request.url.path,
            "completed": False,
            "started_at_utc": utc_now(),
            "database_query_categories": [],
        }
        http_events.append(event)
        context_token = _HTTP_REQUEST_EVENT.set(event)
        try:
            response = await call_next(request)
        except Exception as exc:
            event["error_type"] = type(exc).__name__
            raise
        finally:
            _HTTP_REQUEST_EVENT.reset(context_token)
        event["status"] = response.status_code
        event["completed"] = True
        event["finished_at_utc"] = utc_now()
        return response

    return app


def _selected_tables() -> list[Any]:
    from opendata.models.data_script import DataScript
    from opendata.models.data_table import DataTable
    from opendata.models.interface import DataInterface, InterfaceCategory, InterfaceParameter
    from opendata.models.pipeline import PipelineProgress
    from opendata.models.task import ScheduledTask, TaskExecution
    from opendata.models.user import User

    return [
        User.__table__,
        DataScript.__table__,
        InterfaceCategory.__table__,
        DataInterface.__table__,
        InterfaceParameter.__table__,
        ScheduledTask.__table__,
        TaskExecution.__table__,
        DataTable.__table__,
        PipelineProgress.__table__,
    ]


async def _create_metadata(engine: Any) -> None:
    from opendata.core.database import Base

    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: Base.metadata.create_all(sync, tables=_selected_tables())
        )


async def _seed(engine_factory: Any, secret: str) -> tuple[str, str]:
    from opendata.core.security import create_access_token, hash_password
    from opendata.models.data_script import DataScript, ScriptFrequency
    from opendata.models.interface import DataInterface, InterfaceCategory
    from opendata.models.user import User, UserRole

    async with engine_factory() as session:
        category = InterfaceCategory(
            id=3001,
            name="ac21_fixture",
            description="AC21 synthetic test category",
            sort_order=0,
        )
        active = User(
            id=71001,
            username="ac21_active",
            email="ac21-active@example.com",
            hashed_password=hash_password("AC21_VALID_TEST_PASSWORD"),
            role=UserRole.ADMIN,
            is_active=True,
            is_verified=True,
        )
        inactive = User(
            id=71002,
            username="ac21_inactive",
            email="ac21-inactive@example.com",
            hashed_password=hash_password("AC21_VALID_TEST_PASSWORD"),
            role=UserRole.ADMIN,
            is_active=False,
            is_verified=True,
        )
        script = DataScript(
            id=7,
            script_id="7",
            script_name="AC21_SCRIPT_NUMERIC_COLLISION",
            category="ac21_fixture",
            sub_category="download",
            frequency=ScriptFrequency.MANUAL,
            description="Synthetic AC21 script fixture",
            source="local-test-adapter",
            module_path="ac21.fixture",
            function_name="fetch_rows",
            dependencies={},
            estimated_duration=1,
            timeout=5,
            is_active=True,
            is_custom=False,
        )
        interfaces = [
            DataInterface(
                id=7,
                name="ac21_script_id_collision",
                display_name="AC21_COLLISION_INTERFACE_7",
                description="Counterfactual for script-ID mixup",
                category_id=3001,
                module_path="ac21.fixture",
                function_name="collision",
                parameters={},
                is_active=True,
            ),
            DataInterface(
                id=91,
                name="ac21_selected_success",
                display_name="AC21_SELECTED_INTERFACE_91",
                description="Synthetic row-returning source adapter case",
                category_id=3001,
                module_path="ac21.fixture",
                function_name="success",
                parameters={},
                is_active=True,
            ),
            DataInterface(
                id=92,
                name="ac21_failure",
                display_name="AC21_FAILURE_INTERFACE_92",
                description="Synthetic source failure case",
                category_id=3001,
                module_path="ac21.fixture",
                function_name="failure",
                parameters={},
                is_active=True,
            ),
            DataInterface(
                id=93,
                name="ac21_storage_failure",
                display_name="AC21_STORAGE_FAILURE_INTERFACE_93",
                description="Synthetic incompatible warehouse table failure case",
                category_id=3001,
                module_path="ac21.fixture",
                function_name="storage_failure",
                parameters={},
                is_active=True,
            ),
            DataInterface(
                id=94,
                name="ac21_metadata_commit_failure",
                display_name="AC21_METADATA_FAILURE_INTERFACE_94",
                description="Synthetic metadata transaction failure case",
                category_id=3001,
                module_path="ac21.fixture",
                function_name="metadata_failure",
                parameters={},
                is_active=True,
            ),
            DataInterface(
                id=95,
                name="ac21_empty",
                display_name="AC21_EMPTY_INTERFACE_95",
                description="Synthetic empty-result success case",
                category_id=3001,
                module_path="ac21.fixture",
                function_name="empty",
                parameters={},
                is_active=True,
            ),
        ]
        session.add_all([category, active, inactive, script, *interfaces])
        await session.commit()

    active_token = create_access_token({"sub": "71001", "email": "ac21-active@example.invalid"})
    inactive_token = create_access_token({"sub": "71002", "email": "ac21-inactive@example.invalid"})
    # Secret is installed on the same Settings object used by create_access_token
    # and verify_token; this assertion prevents accidental token/env mismatch.
    from opendata.core.config import settings

    if settings.secret_key != secret:
        raise AssertionError("test settings secret key was not installed before token creation")
    return active_token, inactive_token


async def _read_database(factory: Any, target: Any) -> dict[str, Any]:
    from opendata.models.data_table import DataTable
    from opendata.models.interface import DataInterface
    from opendata.models.task import TaskExecution
    from opendata.utils.helpers import generate_table_name

    async with factory() as session:
        interface_rows = (
            (await session.execute(select(DataInterface).order_by(DataInterface.id)))
            .scalars()
            .all()
        )
        executions = (
            (await session.execute(select(TaskExecution).order_by(TaskExecution.id)))
            .scalars()
            .all()
        )
        tables = (
            (await session.execute(select(DataTable).order_by(DataTable.table_name)))
            .scalars()
            .all()
        )
        execution_records = [
            {
                "id": row.id,
                "execution_id": row.execution_id,
                "script_id": row.script_id,
                "status": row.status.value,
                "start_time": row.start_time.isoformat() if row.start_time else None,
                "end_time": row.end_time.isoformat() if row.end_time else None,
                "duration": float(row.duration) if row.duration is not None else None,
                "rows_after": row.rows_after,
                "error_message": row.error_message,
            }
            for row in executions
        ]
        table_records = [
            {
                "table_name": row.table_name,
                "row_count": row.row_count,
                "last_update_status": row.last_update_status,
            }
            for row in tables
        ]

    table_rows: list[dict[str, Any]] = []
    async with factory() as session:
        fixture_interfaces = (
            (
                await session.execute(
                    select(DataInterface)
                    .where(DataInterface.name.like("ac21_%"))
                    .order_by(DataInterface.id)
                )
            )
            .scalars()
            .all()
        )
        for interface in fixture_interfaces:
            table_name = generate_table_name(interface.name)
            exists_result = await session.execute(
                text(
                    "SELECT COUNT(*) FROM information_schema.tables "
                    "WHERE table_schema = :schema AND table_name = :table_name"
                ),
                {"schema": target.schema, "table_name": table_name},
            )
            exists = bool(exists_result.scalar() or 0)
            row_count = 0
            if exists:
                # table_name comes from the test-only seeded interface name via
                # generate_table_name; identifiers cannot use SQL bind params.
                result = await session.execute(
                    text(f"SELECT COUNT(*) FROM `{table_name}`")  # noqa: S608
                )
                row_count = int(result.scalar() or 0)
            table_rows.append({"table_name": table_name, "exists": exists, "row_count": row_count})

    return {
        "interfaces": [{"id": row.id, "name": row.name} for row in interface_rows],
        "task_executions": execution_records,
        "data_tables": table_records,
        "warehouse_tables": table_rows,
        "mysql_target": target.public_manifest(),
    }


async def _assert_fixture_interfaces_are_unregistered(session_maker: Any, service: Any) -> None:
    from opendata.models.interface import DataInterface

    async with session_maker() as session:
        fixtures = (await session.execute(select(DataInterface))).scalars().all()
    for interface in fixtures:
        assert service._resolve_fetcher(interface) is None


async def _probe_guarded_engine(engine: Any, target: Any) -> None:
    """Exercise the SQLAlchemy connection only after the exact-target guard."""
    from tests.script_download_backend_harness import guard_database_url

    guard_database_url(engine.url, target=target, role="app-metadata")
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))


def test_database_target_guard_rejects_user_urls_before_connect() -> None:
    """Counterfactual guard checks are offline and make zero connection calls."""
    lease = OwnedMysql()
    # This synthetic target exists only to exercise URL policy; no Docker or DB is
    # needed for the negative cases.
    from opendata.core.database import engine as configured_engine

    candidates = [
        configured_engine.url,
        make_url("mysql+aiomysql://counterfactual:synthetic-only@127.0.0.1:3306/opendata"),
        make_url("mysql+aiomysql://counterfactual:synthetic-only@127.0.0.1:33565/opendata_data"),
        make_url("sqlite+aiosqlite:///:memory:"),
    ]

    class NeverConnect:
        def __init__(self, url: Any):
            self.url = url
            self.connect_calls = 0

        def connect(self) -> None:
            self.connect_calls += 1
            raise AssertionError("guard must fail before connecting")

    target = type(
        "Target",
        (),
        {"host_port": 41001, "schema": "ac21_guard_probe", "username": "ac21_guard"},
    )()
    observations = []
    for url in candidates:
        fake = NeverConnect(url)
        with pytest.raises(DatabaseTargetError):
            # This is the same guard-first helper used before engine connect.
            guard_database_url(fake.url, target=target, role="warehouse")
        assert fake.connect_calls == 0
        observations.append({"driver": url.drivername, "connect_calls": fake.connect_calls})
    assert len(observations) == 4
    assert lease.run_id


def test_mysql_start_uses_publishable_bridge_and_loopback_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The isolated Docker network must allow its ephemeral loopback publish."""
    lease = OwnedMysql()
    network_id = "b" * 64
    container_id = "c" * 64
    docker_calls: list[list[str]] = []

    def fake_docker(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        docker_calls.append(args)
        if args[:2] == ["image", "inspect"]:
            stdout = json.dumps([ac21_harness.MYSQL_IMAGE_REFERENCE]) + "\n"
        elif args[:2] == ["network", "create"]:
            stdout = f"{network_id}\n"
        elif args[:2] == ["volume", "create"]:
            stdout = f"{lease.volume_name}\n"
        elif args[0] == "run":
            stdout = f"{container_id}\n"
        elif args[:2] == ["port", container_id]:
            stdout = "127.0.0.1:41002\n"
        else:
            stdout = ""
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(lease, "_docker", fake_docker)
    monkeypatch.setattr(lease, "_wait_ready", lambda: None)

    target = lease.start()

    network_create = next(call for call in docker_calls if call[:2] == ["network", "create"])
    assert "--driver" in network_create
    assert network_create[network_create.index("--driver") + 1] == "bridge"
    assert "--internal" not in network_create
    assert "--label" in network_create
    assert f"codex.owner={lease.owner_label}" in network_create
    assert f"codex.run-id={lease.run_id}" in network_create
    container_run = next(call for call in docker_calls if call[0] == "run")
    assert container_run[container_run.index("--publish") + 1] == "127.0.0.1::3306"
    assert container_run[container_run.index("--network") + 1] == lease.network_name
    assert target.host_port == 41002
    assert target.schema == lease.schema


def test_mysql_diagnostics_redact_passwords_and_omit_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only owned-container logs and selected runtime fields are persisted."""
    lease = OwnedMysql()
    lease.container_id = "synthetic-container-id"
    lease.network_id = "b" * 64
    lease.password = "AC21_TEST_DB_PASSWORD"
    lease.root_password = "AC21_TEST_ROOT_PASSWORD"
    docker_calls: list[list[str]] = []

    def fake_docker(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        docker_calls.append(args)
        if args[0] == "logs":
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=(f"mysql startup {lease.password} {lease.root_password}\n"),
                stderr="",
            )
        if args[0] == "network":
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=(
                    f"{lease.network_id}|{lease.network_name}|bridge|false|"
                    f"{lease.owner_label}|{lease.run_id}"
                ),
                stderr="",
            )
        return subprocess.CompletedProcess(
            args,
            0,
            stdout="synthetic-container-id|running|0||false|started||false",
            stderr="",
        )

    monkeypatch.setattr(lease, "_docker", fake_docker)
    manifest = lease.capture_diagnostics(tmp_path)

    log_text = (tmp_path / "mysql-container-logs-redacted.txt").read_text(encoding="utf-8")
    assert lease.password not in log_text
    assert lease.root_password not in log_text
    assert "[REDACTED]" in log_text
    assert manifest["runtime_inspect"]["result"].startswith("synthetic-container-id|running|0")
    assert manifest["network_inspect"]["captured"] is True
    assert "Env" not in " ".join(" ".join(call) for call in docker_calls)
    assert "credentials_recorded" not in manifest


def test_mysql_cleanup_removes_only_exact_owned_container_network_and_volume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease = OwnedMysql()
    lease.container_id = "c" * 64
    lease.network_id = "b" * 64
    docker_calls: list[list[str]] = []

    def fake_docker(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        docker_calls.append(args)
        if args[0] == "inspect":
            stdout = (
                f"{lease.container_id}|/{lease.container_name}|{lease.owner_label}|{lease.run_id}"
            )
        elif args[:2] == ["volume", "inspect"]:
            stdout = f"{lease.volume_name} {lease.owner_label} {lease.run_id}"
        elif args[:2] == ["network", "inspect"]:
            stdout = (
                f"{lease.network_id}|{lease.network_name}|bridge|false|"
                f"{lease.owner_label}|{lease.run_id}"
            )
        else:
            stdout = ""
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(lease, "_docker", fake_docker)

    assert lease.recover_created_network() == {"attempted": False, "state": "ID_ALREADY_AVAILABLE"}
    removed = lease.cleanup()

    assert removed == {"container": True, "network": True, "volume": True}
    removed_calls = [
        call[:2]
        for call in docker_calls
        if tuple(call[:2]) in {("rm", "--force"), ("volume", "rm"), ("network", "rm")}
    ]
    assert removed_calls == [
        ["rm", "--force"],
        ["volume", "rm"],
        ["network", "rm"],
    ]


@pytest.mark.parametrize("create_response", ["empty", "error"])
def test_mysql_partial_network_create_recovers_exact_owned_network(
    create_response: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recover only the exact daemon-created network after a lost response."""
    lease = OwnedMysql()
    network_id = "b" * 64
    docker_calls: list[list[str]] = []
    daemon_created = False

    def fake_docker(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        nonlocal daemon_created
        docker_calls.append(args)
        if args[:2] == ["image", "inspect"]:
            # The pinned-image guard runs first; the fake must satisfy it so the
            # scenario under test is the partial create, not the digest pin.
            return subprocess.CompletedProcess(
                args, 0, stdout=json.dumps([ac21_harness.MYSQL_IMAGE_REFERENCE]), stderr=""
            )
        if args[:2] == ["network", "create"]:
            daemon_created = True
            if create_response == "error":
                raise DockerCommandError(
                    failure_class="TimeoutError", stage="network-create", return_code=None
                )
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[:2] == ["network", "ls"]:
            output = f"{network_id}|{lease.network_name}\n" if daemon_created else ""
            return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")
        if args[:2] == ["network", "inspect"]:
            output = (
                f"{network_id}|{lease.network_name}|bridge|false|{lease.owner_label}|{lease.run_id}"
            )
            return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(lease, "_docker", fake_docker)
    with pytest.raises((DockerCommandError, RuntimeError)):
        lease.start()

    recovery = lease.recover_created_network()
    cleanup = lease.cleanup()

    assert recovery == {"attempted": True, "state": "RECOVERED"}
    assert lease.network_id == network_id
    assert cleanup == {"container": False, "network": True, "volume": False}
    assert lease.cleanup_errors == []
    listing = next(call for call in docker_calls if call[:2] == ["network", "ls"])
    assert f"label=codex.owner={lease.owner_label}" in listing
    assert f"label=codex.run-id={lease.run_id}" in listing
    assert not any(call[:2] == ["volume", "create"] for call in docker_calls)
    assert [call for call in docker_calls if call[:2] == ["network", "rm"]] == [
        ["network", "rm", network_id]
    ]


@pytest.mark.parametrize(
    ("mismatch", "expected_state"),
    [
        ("listed-name", "NAME_MISMATCH"),
        ("listed-id", "INVALID_ID"),
        ("inspected-name", "IDENTITY_MISMATCH"),
        ("inspected-id", "IDENTITY_MISMATCH"),
        ("owner", "IDENTITY_MISMATCH"),
        ("multiple", "AMBIGUOUS_MATCH"),
    ],
)
def test_mysql_network_recovery_never_removes_wrong_or_ambiguous_identity(
    mismatch: str,
    expected_state: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject malformed IDs and every listed/inspected identity mismatch."""
    lease = OwnedMysql()
    network_id = "c" * 64
    listed_id = "invalid-id" if mismatch == "listed-id" else network_id
    listed_name = "foreign-network" if mismatch == "listed-name" else lease.network_name
    inspected_id = "d" * 64 if mismatch == "inspected-id" else network_id
    inspected_name = "foreign-network" if mismatch == "inspected-name" else lease.network_name
    inspected_owner = "foreign-owner" if mismatch == "owner" else lease.owner_label
    docker_calls: list[list[str]] = []

    def fake_docker(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        docker_calls.append(args)
        if args[:2] == ["network", "ls"]:
            row = f"{listed_id}|{listed_name}\n"
            output = row + row if mismatch == "multiple" else row
            return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")
        if args[:2] == ["network", "inspect"]:
            output = (
                f"{inspected_id}|{inspected_name}|bridge|false|{inspected_owner}|{lease.run_id}"
            )
            return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(lease, "_docker", fake_docker)
    recovery = lease.recover_created_network()
    cleanup = lease.cleanup()

    assert recovery == {"attempted": True, "state": expected_state}
    assert lease.network_id is None
    assert cleanup["network"] is False
    assert lease.network_recovery["state"] == expected_state
    assert not any(call[:2] == ["network", "rm"] for call in docker_calls)
    if mismatch in {"listed-id", "multiple"}:
        assert not any(call[:2] == ["network", "inspect"] for call in docker_calls)


def test_mysql_network_cleanup_failure_is_safe_and_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retain only the known cleanup stage/class when network removal fails."""
    lease = OwnedMysql()
    lease.network_id = "e" * 64

    def fake_docker(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["network", "inspect"]:
            output = (
                f"{lease.network_id}|{lease.network_name}|bridge|false|"
                f"{lease.owner_label}|{lease.run_id}"
            )
            return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")
        if args[:2] == ["network", "rm"]:
            raise DockerCommandError(
                failure_class="OSError", stage="network-remove", return_code=None
            )
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(lease, "_docker", fake_docker)
    cleanup = lease.cleanup()

    assert cleanup == {"container": False, "network": False, "volume": False}
    assert lease.cleanup_errors == [
        {"stage": "network-remove", "failure_class": "OSError", "return_code": None}
    ]


@pytest.mark.parametrize("create_response", ["error", "empty"])
def test_mysql_partial_create_recovers_by_exact_owned_identity(
    create_response: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recover a daemon-created container only after name, ID and labels match."""
    lease = OwnedMysql()
    container_id = "c" * 64
    network_id = "b" * 64
    docker_calls: list[list[str]] = []
    daemon_created = False

    def fake_docker(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        nonlocal daemon_created
        docker_calls.append(args)
        if args[:2] == ["image", "inspect"]:
            # The pinned-image guard runs first; the fake must satisfy it so the
            # scenario under test is the partial create, not the digest pin.
            return subprocess.CompletedProcess(
                args, 0, stdout=json.dumps([ac21_harness.MYSQL_IMAGE_REFERENCE]), stderr=""
            )
        if args[:2] == ["network", "create"]:
            stdout = f"{network_id}\n"
        elif args[:2] == ["volume", "create"]:
            stdout = f"{lease.volume_name}\n"
        elif args[0] == "run":
            daemon_created = True
            if create_response == "error":
                raise DockerCommandError(
                    failure_class="CalledProcessError", stage="container-start", return_code=1
                )
            stdout = ""
        elif args[0] == "ps":
            stdout = f"{container_id}|{lease.container_name}\n" if daemon_created else ""
        elif args[0] == "inspect":
            stdout = f"{container_id}|/{lease.container_name}|{lease.owner_label}|{lease.run_id}"
        elif args[:2] == ["volume", "inspect"]:
            stdout = f"{lease.volume_name} {lease.owner_label} {lease.run_id}"
        elif args[:2] == ["network", "inspect"]:
            stdout = (
                f"{network_id}|{lease.network_name}|bridge|false|{lease.owner_label}|{lease.run_id}"
            )
        else:
            stdout = ""
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(lease, "_docker", fake_docker)
    with pytest.raises((DockerCommandError, RuntimeError)):
        lease.start()

    recovery = lease.recover_created_container()
    ownership = lease.public_manifest()
    cleanup = lease.cleanup()
    receipt = {
        "container_recovery": recovery,
        "resource_ownership": ownership,
        "resource_cleanup": cleanup,
        "cleanup_errors": lease.cleanup_errors,
    }
    assert receipt["container_recovery"] == {"attempted": True, "state": "RECOVERED"}
    assert ownership["container_id"] == container_id
    assert cleanup == {"container": True, "network": True, "volume": True}
    assert lease.cleanup_errors == []
    assert [call for call in docker_calls if call[:2] == ["rm", "--force"]] == [
        ["rm", "--force", container_id]
    ]
    listing_call = next(call for call in docker_calls if call[0] == "ps")
    assert f"label=codex.owner={lease.owner_label}" in listing_call
    assert f"label=codex.run-id={lease.run_id}" in listing_call


@pytest.mark.parametrize("mismatch", ["listed-name", "inspected-name", "owner"])
def test_mysql_partial_create_never_removes_wrong_name_or_owner(
    mismatch: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A name or label mismatch prevents every container removal call."""
    lease = OwnedMysql()
    container_id = "c" * 64
    lease.network_id = None
    listed_name = "another-container" if mismatch == "listed-name" else lease.container_name
    inspected_name = "another-container" if mismatch == "inspected-name" else lease.container_name
    inspected_owner = "wrong-owner" if mismatch == "owner" else lease.owner_label
    docker_calls: list[list[str]] = []

    def fake_docker(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        docker_calls.append(args)
        if args[0] == "ps":
            stdout = f"{container_id}|{listed_name}\n"
        elif args[0] == "inspect":
            stdout = f"{container_id}|/{inspected_name}|{inspected_owner}|{lease.run_id}"
        else:
            stdout = ""
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(lease, "_docker", fake_docker)
    cleanup = lease.cleanup()

    assert cleanup["container"] is False
    assert not any(call[:2] == ["rm", "--force"] for call in docker_calls)
    assert lease.container_id is None
    if mismatch == "listed-name":
        assert lease.container_recovery["state"] == "NAME_MISMATCH"
    else:
        assert lease.container_recovery["state"] == "IDENTITY_MISMATCH"


def test_mysql_cleanup_error_is_preserved_as_a_safe_partial_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cleanup command error becomes a safe partial result and other cleanup continues."""
    lease = OwnedMysql()
    lease.container_id = "c" * 64
    lease.network_id = "b" * 64

    def fake_docker(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        if args[0] == "inspect":
            stdout = (
                f"{lease.container_id}|/{lease.container_name}|{lease.owner_label}|{lease.run_id}"
            )
        elif args[:2] == ["volume", "inspect"]:
            stdout = f"{lease.volume_name} {lease.owner_label} {lease.run_id}"
        elif args[:2] == ["network", "inspect"]:
            stdout = (
                f"{lease.network_id}|{lease.network_name}|bridge|false|"
                f"{lease.owner_label}|{lease.run_id}"
            )
        elif args[:2] == ["rm", "--force"]:
            raise DockerCommandError(
                failure_class="OSError", stage="container-remove", return_code=None
            )
        else:
            stdout = ""
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(lease, "_docker", fake_docker)
    cleanup = lease.cleanup()
    receipt = {"resource_cleanup": cleanup, "cleanup_errors": lease.cleanup_errors}

    assert receipt == {
        "resource_cleanup": {"container": False, "network": True, "volume": True},
        "cleanup_errors": [
            {"stage": "container-remove", "failure_class": "OSError", "return_code": None}
        ],
    }


@pytest.mark.parametrize("port", [3306, 33565])
def test_mysql_loopback_port_rejects_reserved_ports(port: int) -> None:
    with pytest.raises(RuntimeError, match="invalid AC21 loopback port"):
        OwnedMysql._parse_loopback_port(f"127.0.0.1:{port}")


@pytest.mark.integration
def test_real_browser_download_chain_against_owned_mysql(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Click the real UI and execute actual API/background/storage paths offline."""
    from opendata.api import data as data_module
    from opendata.core import database
    from opendata.core.config import settings
    from opendata.models.data_script import DataScript  # noqa: F401
    from opendata.models.data_table import DataTable  # noqa: F401
    from opendata.models.interface import (  # noqa: F401
        InterfaceCategory,
        InterfaceParameter,
    )
    from opendata.models.task import ScheduledTask, TaskExecution  # noqa: F401
    from opendata.models.user import User  # noqa: F401

    run_started = utc_now()
    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{secrets.token_hex(4)}"
    evidence_dir = EVIDENCE_ROOT / "runs" / run_id
    evidence_dir.mkdir(parents=True, exist_ok=True)
    pre_hashes = _file_hashes()
    lease = OwnedMysql()
    server = None
    engine = None
    test_session_maker = None
    source_calls: list[dict[str, Any]] = []
    token_blacklist = None
    backend_http_events: list[dict[str, Any]] = []
    backend_exception_events: list[dict[str, Any]] = []
    run_secret: str | None = None
    active_token: str | None = None
    inactive_token: str | None = None

    def generated_secret_values() -> tuple[str, ...]:
        return tuple(
            value
            for value in (
                lease.password,
                lease.root_password,
                run_secret,
                active_token,
                inactive_token,
            )
            if isinstance(value, str) and value
        )

    def redact_generated_output(value: str) -> str:
        return _redact_generated_text(value, generated_secret_values())[0]

    manifest: dict[str, Any] = {
        "run_id": run_id,
        "started_at_utc": run_started,
        "status": "running",
        "pytest_command": os.environ.get("OPENDATA_AC21_PYTEST_COMMAND"),
        "product_hashes_pre": pre_hashes,
        "harness_hashes_pre": _harness_hashes(),
        "credentials_recorded": False,
        "external_provider_calls_allowed": False,
        "runtime_module_origins": _runtime_module_origins(),
        "test_scope": (
            "ui-errors-only"
            if os.environ.get("OPENDATA_AC21_ERRORS_ONLY") == "1"
            else "full-download-chain"
        ),
    }
    write_json(evidence_dir / "run-manifest.json", manifest)

    try:
        manifest["synthetic_credential_redaction_counterfactual"] = (
            _assert_docker_failure_redaction_counterfactual(monkeypatch)
        )
        target = lease.start()
        manifest["resource_ownership"] = target.public_manifest()
        manifest["mysql_server_options"] = ["--log-bin-trust-function-creators=1"]
        manifest["guard_counterfactuals"] = assert_guard_counterfactuals(target)

        database_url = target.url()
        # Enforce the target URL before create_all/session creation. No settings
        # URL from .env or global core.database engine is used for this app.
        guard_database_url(database_url, target=target, role="app-metadata")
        guard_database_url(database_url, target=target, role="warehouse")
        engine = create_async_engine(database_url, poolclass=NullPool, echo=False)
        test_session_maker = async_sessionmaker(
            engine,
            expire_on_commit=False,
            autoflush=False,
        )
        # Check the actual SQLAlchemy engine URLs before the first connect.
        asyncio.run(_probe_guarded_engine(engine, target))

        asyncio.run(_create_metadata(engine))

        run_secret = secrets.token_urlsafe(48)
        monkeypatch.setattr(settings, "secret_key", run_secret)

        if settings.secret_key != run_secret:
            raise AssertionError("AC21 test secret key was not installed before token creation")

        # The blacklist is replaced by its in-memory implementation so no Redis
        # connection is possible even if a local .env contains REDIS_URL.
        from opendata.core import token_blacklist as token_blacklist_module

        token_blacklist = token_blacklist_module._InMemoryBlacklist()
        monkeypatch.setattr(token_blacklist_module, "token_blacklist", token_blacklist)

        # Both background-task session factories point to the verified lease.
        monkeypatch.setattr(database, "engine", engine)
        monkeypatch.setattr(database, "data_engine", engine)
        monkeypatch.setattr(database, "async_session_maker", test_session_maker)
        monkeypatch.setattr(database, "data_session_maker", test_session_maker)

        def record_http_database_query(
            connection: Any,
            cursor: Any,
            statement: str,
            parameters: Any,
            context: Any,
            executemany: bool,
        ) -> None:
            request_event = _HTTP_REQUEST_EVENT.get()
            if request_event is None:
                return
            normalized = " ".join(statement.lower().split())
            if "users" in normalized:
                category = "identity_lookup"
            elif "data_interfaces" in normalized:
                category = "interface_lookup"
            elif "task_executions" in normalized:
                category = "execution_read_or_write"
            else:
                category = "other_database_io"
            request_event["database_query_categories"].append(category)

        sqlalchemy_event.listen(
            engine.sync_engine,
            "before_cursor_execute",
            record_http_database_query,
        )

        active_token, inactive_token = asyncio.run(_seed(test_session_maker, run_secret))

        adapter_rows = pd.DataFrame(
            [
                {"symbol": "AC21A", "close": 12.34},
                {"symbol": "AC21B", "close": 56.78},
            ]
        )
        raw_data_json = adapter_rows.to_json(orient="records", date_format="iso")
        raw_data_hash = hashlib.sha256(raw_data_json.encode("utf-8")).hexdigest()
        storage_rows = pd.DataFrame({"payload": ["synthetic AC21 storage row"]})
        storage_data_hash = hashlib.sha256(
            storage_rows.to_json(orient="records", date_format="iso").encode("utf-8")
        ).hexdigest()

        async def offline_source_adapter(
            interface: DataInterface, parameters: dict[str, Any], timeout: int | None = None
        ) -> _FrameCopy:
            allowed_names = {
                "ac21_selected_success",
                "ac21_failure",
                "ac21_storage_failure",
                "ac21_metadata_commit_failure",
                "ac21_empty",
            }
            if interface.name not in allowed_names:
                raise AssertionError("unexpected interface reached AC21 source adapter")
            rows_by_name: dict[str, _FrameCopy | None] = {
                "ac21_selected_success": adapter_rows,
                "ac21_failure": None,
                "ac21_storage_failure": storage_rows,
                "ac21_metadata_commit_failure": adapter_rows,
                "ac21_empty": pd.DataFrame(),
            }
            result = rows_by_name[interface.name]
            source_calls.append(
                {
                    "interface_id": interface.id,
                    "interface_name": interface.name,
                    "recorded_at_utc": utc_now(),
                    "parameters": parameters,
                    "raw_rows": len(result) if result is not None else 0,
                    "raw_data_sha256": (
                        raw_data_hash
                        if interface.name
                        in {"ac21_selected_success", "ac21_metadata_commit_failure"}
                        else storage_data_hash
                        if interface.name == "ac21_storage_failure"
                        else None
                    ),
                    "mode": "offline-deterministic-source-adapter",
                }
            )
            if interface.name == "ac21_failure":
                raise RuntimeError("AC21_SYNTHETIC_SOURCE_FAILURE")
            if result is None:
                raise AssertionError("non-failure source fixture unexpectedly returned no rows")
            return result.copy(deep=True)

        # Names must resolve to no registered provider fetcher; the only source
        # seam is the lowest legacy/upstream call function below this assertion.
        asyncio.run(
            _assert_fixture_interfaces_are_unregistered(
                test_session_maker, data_module.data_service
            )
        )
        monkeypatch.setattr(
            data_module.data_service, "_call_akshare_function", offline_source_adapter
        )

        original_execute_download = data_module.data_service.execute_download

        def redact_backend_detail(value: str) -> str:
            redacted = lease._redact(value)
            for secret in (active_token, inactive_token, run_secret):
                if secret:
                    redacted = redacted.replace(secret, "[REDACTED]")
            return redacted

        async def record_service_exception(**kwargs: Any) -> int | None:
            try:
                return await original_execute_download(**kwargs)
            except Exception as exc:
                details = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
                backend_exception_events.append(
                    {
                        "occurred_at_utc": utc_now(),
                        "execution_id": kwargs.get("execution_id"),
                        "interface_id": kwargs.get("interface_id"),
                        "exception_type": type(exc).__name__,
                        "message": redact_backend_detail(str(exc)),
                        "traceback": redact_backend_detail(details),
                    }
                )
                raise

        monkeypatch.setattr(data_module.data_service, "execute_download", record_service_exception)

        # Synthetic schema defects exercise actual warehouse DDL/DML failure,
        # while the metadata trigger exercises a real DataTable commit failure.
        # Both failpoints exist only inside this newly created run schema.
        from opendata.utils.helpers import generate_table_name

        storage_failure_table = generate_table_name("ac21_storage_failure")
        metadata_failure_table = generate_table_name("ac21_metadata_commit_failure")

        async def _install_mysql_failpoints() -> None:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        f"CREATE TABLE `{storage_failure_table}` "
                        "(`id` BIGINT AUTO_INCREMENT PRIMARY KEY) ENGINE=InnoDB"
                    )
                )
                await connection.execute(
                    text(
                        "CREATE TRIGGER `ac21_fail_metadata_insert` "
                        "BEFORE INSERT ON `data_tables` FOR EACH ROW "
                        f"BEGIN IF NEW.table_name = '{metadata_failure_table}' THEN "
                        "SIGNAL SQLSTATE '45000' "
                        "SET MESSAGE_TEXT = 'AC21_SYNTHETIC_METADATA_COMMIT_FAILURE'; "
                        "END IF; END"
                    )
                )

        asyncio.run(_install_mysql_failpoints())
        manifest["storage_failpoint"] = {
            "table_name": storage_failure_table,
            "setup": "test-owned incomplete table causes real INSERT unknown-column error",
        }
        manifest["metadata_failpoint"] = {
            "table_name": metadata_failure_table,
            "setup": "test-owned BEFORE INSERT trigger rejects only this DataTable row",
        }

        backend_port = _free_loopback_port()
        app = _create_test_app(backend_http_events, test_session_maker)
        server, server_thread, backend_url = _serve(app, backend_port)
        manifest["backend"] = {"url": backend_url, "host": "127.0.0.1", "lifespan": "off"}

        frontend_port = _free_loopback_port()
        env = {
            **os.environ,
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "OPENDATA_AC21_BACKEND_URL": backend_url,
            "OPENDATA_AC21_FRONTEND_PORT": str(frontend_port),
            "OPENDATA_AC21_EVIDENCE_DIR": str(evidence_dir),
            "OPENDATA_AC21_ACTIVE_TOKEN": active_token,
            "OPENDATA_AC21_INACTIVE_TOKEN": inactive_token,
        }
        playwright_command = [
            str(FRONTEND / "node_modules/.bin/playwright"),
            "test",
            "--config=e2e/script-download-backend.playwright.config.ts",
            "e2e/script-download-backend.spec.ts",
            "--reporter=line",
        ]
        manifest["playwright_command"] = " ".join(
            [
                "node_modules/.bin/playwright",
                "test",
                "--config=e2e/script-download-backend.playwright.config.ts",
                "e2e/script-download-backend.spec.ts",
                "--reporter=line",
            ]
        )
        manifest["frontend"] = {"url": f"http://127.0.0.1:{frontend_port}", "host": "127.0.0.1"}
        write_json(evidence_dir / "run-manifest.json", manifest)

        # Absolute executable and argv are fixed by this repository-owned test.
        playwright = subprocess.run(  # noqa: S603
            playwright_command,
            cwd=FRONTEND,
            env=env,
            capture_output=True,
            text=True,
            timeout=240,
        )
        playwright_output = redact_generated_output(
            playwright.stdout + "\n--- STDERR ---\n" + playwright.stderr
        )
        (evidence_dir / "playwright.log").write_text(playwright_output, encoding="utf-8")
        manifest["playwright_exit_code"] = playwright.returncode
        manifest["playwright_stdout_tail"] = redact_generated_output(playwright.stdout[-12000:])
        manifest["playwright_stderr_tail"] = redact_generated_output(playwright.stderr[-12000:])

        # The browser path records the accepted execution IDs; query the real DB
        # and warehouse table before the container is destroyed.
        database_state = asyncio.run(_read_database(test_session_maker, target))
        manifest["database_observed"] = database_state
        manifest["backend_http_events"] = backend_http_events
        manifest["backend_exceptions"] = backend_exception_events
        manifest["source_adapter_calls"] = source_calls
        manifest["browser_trace_exists"] = (evidence_dir / "browser-trace.json").exists()
        if manifest["browser_trace_exists"]:
            trace = json.loads((evidence_dir / "browser-trace.json").read_text(encoding="utf-8"))
            manifest["browser_trace_summary"] = {
                "api_event_count": len(trace.get("api_events", [])),
                "blocked_external_origins": trace.get("blocked_external_origins", []),
                "browser_errors": trace.get("browser_errors", []),
                "client_navigation": trace.get("client_navigation", []),
                "final_url": trace.get("final_url"),
                "ui_clicks": trace.get("ui_clicks", []),
                "observed_status": trace.get("observed_status", {}),
                "ui_status_display_ms": trace.get("ui_status_display_ms", {}),
                "auth_and_validation": trace.get("auth_and_validation", {}),
                "ui_error_cases": trace.get("ui_error_cases", []),
                "login_error_counterfact": trace.get("login_error_counterfact"),
                "credentials_recorded": trace.get("credentials_recorded"),
            }

        # Preserve the browser trace and distinguish the frontend's natural
        # navigation result from the backend task-state observations.
        trace_text = (
            (evidence_dir / "browser-trace.json").read_text(encoding="utf-8")
            if (evidence_dir / "browser-trace.json").exists()
            else ""
        )
        if active_token in trace_text or inactive_token in trace_text:
            raise AssertionError("browser trace contains an authentication token")
        if "wrong-ac21-password" in trace_text or "AC21_VALID_TEST_PASSWORD" in trace_text:
            raise AssertionError("browser trace contains a test password")
        assert manifest["browser_trace_exists"], "Browser run did not produce its redacted trace"
        errors_only = os.environ.get("OPENDATA_AC21_ERRORS_ONLY") == "1"
        if errors_only:
            assert source_calls == []
            assert database_state["task_executions"] == []
        else:
            assert {call["interface_id"] for call in source_calls} == {91, 92, 93, 94, 95}
            assert len(database_state["task_executions"]) == 5
            assert database_state["task_executions"]
            executions_by_interface = {
                item["script_id"]: item for item in database_state["task_executions"]
            }
            assert executions_by_interface["ac21_selected_success"]["status"] == "completed"
            assert executions_by_interface["ac21_failure"]["status"] == "failed"
            assert executions_by_interface["ac21_storage_failure"]["status"] == "failed"
            assert executions_by_interface["ac21_metadata_commit_failure"]["status"] == "failed"
            assert executions_by_interface["ac21_empty"]["status"] == "completed"
            for interface_name, execution in executions_by_interface.items():
                assert execution["start_time"] is not None, interface_name
                assert execution["end_time"] is not None, interface_name
                assert execution["duration"] is not None, interface_name
            assert executions_by_interface["ac21_selected_success"]["rows_after"] == 2
            assert executions_by_interface["ac21_empty"]["rows_after"] == 0
            assert executions_by_interface["ac21_failure"]["error_message"]
            assert executions_by_interface["ac21_storage_failure"]["error_message"]
            assert executions_by_interface["ac21_metadata_commit_failure"]["error_message"]
            success_metadata = next(
                row
                for row in database_state["data_tables"]
                if row["table_name"] == "ak_ac21_selected_success"
            )
            assert success_metadata["row_count"] == 2
            success_warehouse = next(
                row
                for row in database_state["warehouse_tables"]
                if row["table_name"] == "ak_ac21_selected_success"
            )
            assert success_warehouse["exists"] and success_warehouse["row_count"] == 2
            storage_warehouse = next(
                row
                for row in database_state["warehouse_tables"]
                if row["table_name"] == "ak_ac21_storage_failure"
            )
            assert storage_warehouse["exists"] and storage_warehouse["row_count"] == 0
            metadata_failure_warehouse = next(
                row
                for row in database_state["warehouse_tables"]
                if row["table_name"] == "ak_ac21_metadata_commit_failure"
            )
            assert metadata_failure_warehouse["exists"]
            assert metadata_failure_warehouse["row_count"] == 2
            assert all(
                row["table_name"] != "ak_ac21_metadata_commit_failure"
                for row in database_state["data_tables"]
            )

        trace_summary = manifest["browser_trace_summary"]
        expected_statuses = (
            {}
            if errors_only
            else {
                "nonempty_success": "completed",
                "source_failure": "failed",
                "storage_failure": "failed",
                "metadata_commit_failure": "failed",
                "empty_success": "completed",
            }
        )
        assert trace_summary["observed_status"] == expected_statuses
        expected_ui_error_statuses = {
            "inactive_user_403": 403,
            "missing_interface_id_422": 422,
            "unknown_interface_404": 404,
            "unauthenticated_401": 401,
        }
        observed_ui_errors = trace_summary["ui_error_cases"]
        assert [item["label"] for item in observed_ui_errors] == list(expected_ui_error_statuses)
        assert {item["label"]: item["observed_http"] for item in observed_ui_errors} == (
            expected_ui_error_statuses
        )
        source_call_times = [
            datetime.fromisoformat(call["recorded_at_utc"].replace("Z", "+00:00"))
            for call in source_calls
        ]
        ui_error_counterfactuals = []
        for case in observed_ui_errors:
            case_start = datetime.fromtimestamp(case["started_at_epoch_ms"] / 1000, timezone.utc)
            case_end = datetime.fromtimestamp(case["finished_at_epoch_ms"] / 1000, timezone.utc)
            matching_http_events = [
                event
                for event in backend_http_events
                if event["method"] == "POST"
                and event["path"] == "/api/v1/data/download"
                and event.get("status") == case["observed_http"]
                and case_start
                <= datetime.fromisoformat(event["started_at_utc"].replace("Z", "+00:00"))
                <= case_end
            ]
            assert len(matching_http_events) == 1, case["label"]
            http_event = matching_http_events[0]
            source_calls_during_case = sum(case_start <= at <= case_end for at in source_call_times)
            db_categories = http_event["database_query_categories"]
            identity_queries = db_categories.count("identity_lookup")
            expected_identity_queries = 0 if case["label"] == "unauthenticated_401" else 1
            counterfactual = {
                "label": case["label"],
                "expected_http": case["expected_http"],
                "observed_http": http_event["status"],
                "expected_task_delta": 0,
                "observed_task_delta": case["observed_task_delta"],
                "execution_count_before": case["task_count_before"],
                "execution_count_after": case["task_count_after"],
                "expected_source_adapter_calls_during_case": 0,
                "observed_source_adapter_calls_during_case": source_calls_during_case,
                "database_query_categories": db_categories,
                "expected_identity_lookup_queries": expected_identity_queries,
                "observed_identity_lookup_queries": identity_queries,
                "expected_interface_lookup": case["label"] == "unknown_interface_404",
                "observed_interface_lookup": "interface_lookup" in db_categories,
                "expected_execution_write": False,
                "observed_execution_write": "execution_read_or_write" in db_categories,
                "expected_feedback": case["expected_feedback"],
                "observed_feedback": case["visible_feedback"],
                "feedback_matched": case["feedback_matched"],
                "success_toast_visible": case["success_toast_visible"],
                "credentials_recorded": False,
            }
            ui_error_counterfactuals.append(counterfactual)
            assert case["expected_http"] == http_event["status"], case["label"]
            assert case["observed_task_delta"] == 0, case["label"]
            assert case["task_count_before"] == case["task_count_after"], case["label"]
            assert source_calls_during_case == 0, case["label"]
            assert identity_queries == expected_identity_queries, counterfactual
            assert ("interface_lookup" in db_categories) == counterfactual[
                "expected_interface_lookup"
            ], counterfactual
            assert "execution_read_or_write" not in db_categories, counterfactual
            assert case["success_toast_visible"] is False, case["label"]
        manifest["ui_error_counterfactuals"] = ui_error_counterfactuals
        expected_status_display_cases = (
            set()
            if errors_only
            else {
                "nonempty_success",
                "source_failure",
                "storage_failure",
                "metadata_commit_failure",
                "empty_success",
            }
        )
        assert set(trace_summary["ui_status_display_ms"]) == expected_status_display_cases
        assert all(value >= 0 for value in trace_summary["ui_status_display_ms"].values())
        assert trace_summary["blocked_external_origins"] == []

        login_case = trace_summary["login_error_counterfact"]
        assert login_case is not None
        login_start = datetime.fromtimestamp(login_case["started_at_epoch_ms"] / 1000, timezone.utc)
        login_end = datetime.fromtimestamp(login_case["finished_at_epoch_ms"] / 1000, timezone.utc)
        matching_login_events = [
            event
            for event in backend_http_events
            if event["method"] == "POST"
            and event["path"] == "/api/v1/auth/login"
            and event.get("status") == 401
            and login_start
            <= datetime.fromisoformat(event["started_at_utc"].replace("Z", "+00:00"))
            <= login_end
        ]
        assert len(matching_login_events) == 1
        login_http_event = matching_login_events[0]
        login_db_categories = login_http_event["database_query_categories"]
        login_identity_queries = login_db_categories.count("identity_lookup")
        login_source_calls = sum(login_start <= at <= login_end for at in source_call_times)
        login_refresh_requests = [
            event
            for event in backend_http_events
            if event["method"] == "POST"
            and event["path"] == "/api/v1/auth/refresh"
            and login_start
            <= datetime.fromisoformat(event["started_at_utc"].replace("Z", "+00:00"))
            <= login_end
        ]
        login_counterfactual = {
            "label": login_case["label"],
            "expected_http": login_case["expected_http"],
            "observed_http": login_http_event["status"],
            "expected_auth_identity_lookups": 1,
            "observed_auth_identity_lookups": login_identity_queries,
            "expected_refresh_requests": 0,
            "observed_refresh_requests": len(login_refresh_requests),
            "observed_api_requests_during_login_action": login_case["api_requests_during_request"],
            "expected_login_api_requests_during_action": ["POST /api/v1/auth/login"],
            "database_query_categories": login_db_categories,
            "expected_execution_delta": 0,
            "observed_execution_delta": login_case["observed_task_delta"],
            "expected_source_adapter_calls": 0,
            "observed_source_adapter_calls": login_source_calls,
            "expected_visible_feedback": "邮箱或密码错误",
            "observed_visible_feedback": login_case["visible_feedback"],
            "persistent_session_expired_notice_visible": login_case[
                "persistent_session_expired_notice_visible"
            ],
            "navigation_count_during_request": login_case["navigation_count_during_request"],
            "route_after": f"{login_case['pathname_after']}{login_case['query_after']}",
            "request_body_recorded": login_case["request_body_recorded"],
            "credentials_recorded": login_case["credentials_recorded"],
        }
        manifest["login_error_counterfactual"] = login_counterfactual
        assert login_case["expected_http"] == login_http_event["status"] == 401
        assert login_identity_queries == 1, login_counterfactual
        assert "execution_read_or_write" not in login_db_categories, login_counterfactual
        assert len(login_refresh_requests) == 0, login_counterfactual
        assert login_case["api_requests_during_request"] == ["POST /api/v1/auth/login"]
        assert login_case["visible_feedback"] == ["邮箱或密码错误"]
        assert login_case["persistent_session_expired_notice_visible"] is True
        assert login_case["pathname_after"] == "/login"
        assert login_case["query_after"] == "?reason=session_expired"
        assert login_case["navigation_count_during_request"] == 0
        assert login_case["observed_task_delta"] == 0
        assert login_case["task_count_before"] == login_case["task_count_after"]
        assert login_case["request_body_recorded"] is False
        assert login_case["credentials_recorded"] is False
        assert login_source_calls == 0, login_counterfactual

        navigation_failures = (
            []
            if errors_only
            else [
                event
                for event in trace_summary["client_navigation"]
                if not event["ui_navigated_to_executions"]
            ]
        )
        if navigation_failures:
            assert playwright.returncode != 0
            assert "AC21 natural UI navigation failed" in playwright.stdout
            manifest["status"] = "BACKEND_CHAIN_PASS_UI_NAVIGATION_FAIL"
            manifest["finding"] = (
                "All real API/task/storage/auth probes passed, but ScriptDetailView did not "
                "naturally navigate to /executions after an accepted download. The browser "
                "used explicit navigation only to continue observing the actual task."
            )
            pytest.fail(
                "AC21 backend chain passed, but natural UI navigation remains a separate "
                f"failure: {navigation_failures}. See {evidence_dir / 'run-manifest.json'}"
            )

        feedback_failures = [case for case in observed_ui_errors if not case["feedback_matched"]]
        if feedback_failures:
            manifest["status"] = "UI_ERROR_FEEDBACK_FAIL"
            manifest["finding"] = {
                "expected": "Every rejected UI download shows its specific visible error feedback",
                "failed_cases": [
                    {
                        "label": case["label"],
                        "http_status": case["observed_http"],
                        "expected_feedback": case["expected_feedback"],
                        "visible_feedback": case["visible_feedback"],
                    }
                    for case in feedback_failures
                ],
            }
            write_json(evidence_dir / "run-manifest.json", manifest)
            pytest.fail(
                "Real browser/backend download-error path returned the expected HTTP status, "
                "but visible feedback is missing: "
                f"{[case['label'] for case in feedback_failures]}. "
                f"See {evidence_dir / 'browser-trace.json'}"
            )

        assert playwright.returncode == 0, redact_generated_output(playwright.stdout[-4000:])
        manifest["status"] = "BACKEND_CHAIN_PASS"
        manifest["finding"] = None

    except BaseException as exc:
        if manifest.get("status") == "running":
            manifest["status"] = "INFRA_ERROR"
        manifest["exception"] = _safe_exception_record(exc)
        raise
    finally:
        if server is not None:
            server.should_exit = True
            server_thread.join(timeout=10)
        if token_blacklist is not None:
            token_blacklist.stop()
        if engine is not None:
            asyncio.run(engine.dispose())
        manifest["container_recovery"] = lease.recover_created_container()
        manifest["network_recovery"] = lease.recover_created_network()
        manifest["resource_ownership"] = lease.public_manifest()
        try:
            manifest["docker_diagnostics"] = lease.capture_diagnostics(evidence_dir)
        except Exception as diagnostics_error:
            # Preserve cleanup and the original test failure if diagnostic
            # collection itself encounters an issue; do not persist error text.
            manifest["docker_diagnostics"] = {
                "captured": False,
                "capture_error_type": type(diagnostics_error).__name__,
            }
        cleanup = lease.cleanup()
        manifest["resource_cleanup"] = cleanup
        manifest["cleanup_errors"] = list(lease.cleanup_errors)
        exception_evidence = {
            "source": "actual DataAcquisitionService.execute_download exception boundary",
            "exceptions": backend_exception_events,
            "credentials_recorded": False,
        }
        write_json(evidence_dir / "backend-exceptions-redacted.json", exception_evidence)
        manifest["backend_exception_evidence"] = {
            "path": "backend-exceptions-redacted.json",
            "count": len(backend_exception_events),
            "credentials_recorded": False,
        }
        manifest["ended_at_utc"] = utc_now()
        manifest["product_hashes_post"] = _file_hashes()
        manifest["product_files_unchanged"] = (
            manifest["product_hashes_pre"] == manifest["product_hashes_post"]
        )
        manifest["harness_hashes_post"] = _harness_hashes()
        manifest["harness_files_unchanged"] = (
            manifest["harness_hashes_pre"] == manifest["harness_hashes_post"]
        )
        manifest["backend_exceptions"] = backend_exception_events
        manifest["runtime_module_origins"] = _runtime_module_origins()
        manifest["credentials_recorded"] = False
        generated_secrets = generated_secret_values()
        redaction_summary = _redact_evidence_files(evidence_dir, generated_secrets)
        manifest["generated_value_scan"] = redaction_summary
        serialized_manifest = json.dumps(manifest, sort_keys=True, default=str)
        serialized_manifest, manifest_replacements = _redact_generated_text(
            serialized_manifest, generated_secrets
        )
        if any(secret in serialized_manifest for secret in generated_secrets):
            raise AssertionError("generated credential remained in final manifest serialization")
        safe_manifest = json.loads(serialized_manifest)
        if not isinstance(safe_manifest, dict):
            raise AssertionError("final redacted manifest is not a JSON object")
        safe_manifest["generated_value_scan"] = {
            **redaction_summary,
            "final_write_replacement_count": manifest_replacements,
            "final_scan_clean": True,
        }
        final_manifest_text = json.dumps(safe_manifest, sort_keys=True, default=str)
        if any(secret in final_manifest_text for secret in generated_secrets):
            raise AssertionError("generated credential remained after final manifest metadata")
        manifest = safe_manifest
        write_json(evidence_dir / "run-manifest.json", manifest)
