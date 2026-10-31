"""Offline replay of six previously captured real Fuyao financial responses.

The private samples are read only from an explicitly supplied directory, never
copied into the repository. Each captured ``.entity`` file is size- and
SHA-256-checked against its capture manifest, then replayed through the real
reviewed Fuyao job and its fixed ``received_at_utc`` observation time into a
single test-owned SQLite financial-observation archive. The real archive route
is mounted with a read-only SQLite SELECT in place of its MySQL page loader,
and AFI's real ``OpenDataFinancialArchiveReader`` consumes every scope over
HTTPX MockTransport.

This is a replay of previously captured bytes, not a live request. It does not
establish source rights, retention, point-in-time or revision history, full
history, production MySQL behavior, deployment, or a naturally scheduled cycle.
The test is opt-in and hermetic: it runs with ``--noconftest``, removes live
provider/database environment variables, denies sockets and non-mock HTTP
transports, and keeps all generated files under pytest's private temp root.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import importlib.util
import json
import os
import socket
import stat
import sys
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import and_, event, func, select
from sqlalchemy.pool import NullPool

_INSTRUMENT = "600519.SH"
_SOURCE_ID = "ths_fuyao"
_STATEMENTS = ("income", "balance", "cashflow")
_PERIODICITIES = ("annual", "quarterly")
_STATEMENT_ENDPOINTS = {
    "income": "/api/a-share/financials/income-statements",
    "balance": "/api/a-share/financials/balance-sheets",
    "cashflow": "/api/a-share/financials/cash-flow-statements",
}
_STATEMENT_ITEMS = {"income": 13, "balance": 7, "cashflow": 6}
_MAX_ENTITY_BYTES = 8 * 1024 * 1024
_MAX_MANIFESTS = 2
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_ROW_METADATA_FIELDS = frozenset(
    {
        "thscode",
        "ticker",
        "period",
        "fiscal_year",
        "fiscal_period",
        "report_date_ms",
        "period_end_ms",
        "currency",
    }
)
_LIVE_ENVIRONMENT_NAMES = (
    "FINANCIAL_ARCHIVE_DATABASE_URL",
    "DATABASE_URL",
    "DATABASE_URL_SYNC",
    "DATA_DATABASE_URL",
    "REDIS_URL",
    "SENTRY_DSN",
    "FUYAO_API_KEY",
    "OPENDATA_FUYAO_PROCESS_BINDING_JSON",
    "OPENDATA_API_KEY",
    "OPENDATA_BASE_URL",
    "STOCK_ANALYSIS_OPENDATA_FINANCIAL_ARCHIVE_ALLOWED_HOSTS",
    "MYSQL_PWD",
)
_ALLOWED_TEST_ENVIRONMENT_NAMES = frozenset(
    {
        "APP_ENV",
        "TESTING",
        "REDIS_URL",
        "SENTRY_DSN",
        "FUYAO_API_KEY",
        "OPENDATA_FUYAO_PROCESS_BINDING_JSON",
    }
)


def _deny_network(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("network access is disabled during the offline replay")


async def _deny_async_network(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("network access is disabled during the offline replay")


@pytest.fixture(autouse=True)
def hermetic_replay_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    """Restrict environment, cwd, and network before any repository import."""
    for name in _LIVE_ENVIRONMENT_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("APP_ENV", "testing")
    monkeypatch.setenv("TESTING", "true")
    monkeypatch.setenv("REDIS_URL", "")
    monkeypatch.setenv("SENTRY_DSN", "")
    monkeypatch.chdir(tmp_path)
    assert not (tmp_path / ".env").exists()

    monkeypatch.setattr(socket.socket, "connect", _deny_network)
    monkeypatch.setattr(socket.socket, "connect_ex", _deny_network)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _deny_network)
    monkeypatch.setattr(
        httpx.AsyncHTTPTransport,
        "handle_async_request",
        _deny_async_network,
    )
    baseline_names = set(os.environ)
    yield tmp_path
    added_names = set(os.environ) - baseline_names
    unexpected = added_names - _ALLOWED_TEST_ENVIRONMENT_NAMES
    assert not unexpected, f"test introduced non-allowlisted environment names: {unexpected}"


def _load_existing_job_test_support() -> ModuleType:
    """Reuse the established synthetic gate, bundle, and response helpers."""
    path = Path(__file__).with_name("test_fuyao_financial_run_job.py")
    spec = importlib.util.spec_from_file_location(
        "_opendata_fuyao_six_replay_job_test_support",
        path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("existing Fuyao job test support is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def _load_afi_reader_types(backend_root: Path) -> tuple[type[Any], type[Any]]:
    """Load only AFI credentials and archive reader, bypassing application setup."""
    app_root = backend_root / "app"
    reader_path = app_root / "services/stock_analysis/opendata_financial_archive.py"
    credentials_path = app_root / "services/market_data/opendata_credentials.py"
    if not reader_path.is_file() or not credentials_path.is_file():
        pytest.fail(
            "AFI checkout snapshot is incomplete: expected the financial archive reader and "
            "OpenData credentials modules under src/backend/app"
        )

    previous = {
        name: module
        for name, module in tuple(sys.modules.items())
        if name == "app" or name.startswith("app.")
    }
    for name in previous:
        sys.modules.pop(name, None)

    app_package = ModuleType("app")
    app_package.__package__ = "app"
    app_package.__path__ = [str(app_root)]
    sys.modules["app"] = app_package
    try:
        credentials_module = importlib.import_module(
            "app.services.market_data.opendata_credentials"
        )
        reader_module = importlib.import_module(
            "app.services.stock_analysis.opendata_financial_archive"
        )
        return (
            credentials_module.OpendataCredentials,
            reader_module.OpenDataFinancialArchiveReader,
        )
    finally:
        for name in tuple(sys.modules):
            if name == "app" or name.startswith("app."):
                sys.modules.pop(name, None)
        sys.modules.update(previous)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON keys in a private sample entity."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _utc_datetime(value: object, label: str) -> datetime:
    """Parse one manifest instant and require an explicit zero UTC offset."""
    if not isinstance(value, str):
        pytest.fail(f"{label} is not a timestamp string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        pytest.fail(f"{label} is not an ISO-8601 timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.utcoffset().total_seconds():
        pytest.fail(f"{label} is not explicitly UTC")
    return parsed.astimezone(timezone.utc)


def _utc_iso_z(value: datetime) -> str:
    """Render one UTC instant exactly as the archive route does."""
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _shanghai_date(millis: object, label: str) -> date:
    """Interpret an exact source epoch-millisecond stamp at Shanghai midnight."""
    if type(millis) is not int:
        pytest.fail(f"{label} is not an integer epoch-millisecond stamp")
    try:
        return datetime.fromtimestamp(millis / 1000, tz=_SHANGHAI).date()
    except (OverflowError, OSError, ValueError):
        pytest.fail(f"{label} is not a usable epoch-millisecond stamp")


def _storage_decimal_text(token: str) -> str:
    """Mirror the archive's exact non-exponent decimal storage rendering."""
    try:
        value = Decimal(token)
    except Exception:
        pytest.fail("a captured financial value is not a decimal token")
    if not value.is_finite():
        pytest.fail("a captured financial value is not finite")
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    if rendered in {"", "-0", "+0"}:
        return "0"
    return rendered[1:] if rendered.startswith("+") else rendered


def _read_regular_entity(path: Path, expected_size: int) -> bytes:
    """Open one non-symlink regular file with O_NOFOLLOW and read it bounded."""
    if expected_size <= 0 or expected_size > _MAX_ENTITY_BYTES:
        pytest.fail("capture manifest declares an out-of-bounds entity size")
    try:
        before = path.lstat()
    except OSError:
        pytest.fail("capture entity file is missing")
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        pytest.fail("capture entity is not a regular non-symlink file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        pytest.fail("capture entity could not be opened without following links")
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size != expected_size:
            pytest.fail("capture entity changed or does not match its manifest size")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65_536, expected_size + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > expected_size:
                pytest.fail("capture entity exceeds its manifest size")
    finally:
        os.close(descriptor)
    if total != expected_size:
        pytest.fail("capture entity is shorter than its manifest size")
    return b"".join(chunks)


def _expected_values(
    body: bytes,
    *,
    statement_type: str,
    periodicity: str,
) -> dict[str, Any]:
    """Parse one captured entity only to derive its exact expected observations."""
    try:
        payload = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_float=str,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        pytest.fail("capture entity is not valid UTF-8 JSON")
    if (
        not isinstance(payload, dict)
        or payload.get("code") != 0
        or not isinstance(payload.get("data"), dict)
    ):
        pytest.fail("capture entity is not a successful Fuyao envelope")
    data = payload["data"]
    rows = data.get("item")
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        pytest.fail("capture entity must contain exactly one financial row")
    row = rows[0]
    if row.get("thscode") != _INSTRUMENT or row.get("period") != periodicity:
        pytest.fail("capture entity scope does not match its manifest")
    if row.get("currency") != "CNY":
        pytest.fail("capture entity currency is not the expected CNY")

    from opendata.data.providers.ths.endpoints import financial_statement_items

    items = financial_statement_items(statement_type)
    if len(items) != _STATEMENT_ITEMS[statement_type]:
        pytest.fail("financial statement mapping item count changed")
    if set(row).difference(_ROW_METADATA_FIELDS) != set(items):
        pytest.fail("capture entity item set does not match the reviewed mapping")

    period_end_ms = row.get("period_end_ms")
    report_date_ms = row.get("report_date_ms")
    report_period = _shanghai_date(period_end_ms, "period_end_ms")
    disclosure_date = _shanghai_date(report_date_ms, "report_date_ms")
    if data.get("timestamp") != period_end_ms:
        pytest.fail("capture envelope timestamp does not equal the row period end")

    values: dict[str, str] = {}
    for item in items:
        token = row.get(item)
        if not isinstance(token, str):
            pytest.fail(f"capture item {item} is not a preserved number token")
        values[item] = _storage_decimal_text(token)
    return {
        "items": tuple(items),
        "values": values,
        "report_period": report_period.isoformat(),
        "disclosure_date": disclosure_date.isoformat(),
    }


def _load_cases(sample_root: Path) -> list[dict[str, Any]]:
    """Validate both manifests and every completed entity before any replay."""
    if not sample_root.is_absolute() or not sample_root.is_dir():
        pytest.fail("FUYAO_SIX_REPLAY_SAMPLE_DIR must be an absolute existing directory")
    manifest_paths = sorted(sample_root.rglob("manifest.json"))
    if len(manifest_paths) != _MAX_MANIFESTS:
        pytest.fail(
            f"expected exactly {_MAX_MANIFESTS} capture manifests, found {len(manifest_paths)}"
        )

    documents: list[tuple[Path, dict[str, Any], str]] = []
    for manifest_path in manifest_paths:
        if manifest_path.is_symlink() or not manifest_path.is_file():
            pytest.fail("capture manifest is not a regular non-symlink file")
        raw_manifest = manifest_path.read_bytes()
        try:
            document = json.loads(raw_manifest.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            pytest.fail("capture manifest is not valid UTF-8 JSON")
        if not isinstance(document, dict) or not isinstance(document.get("requests"), list):
            pytest.fail("capture manifest has no request list")
        documents.append((manifest_path, document, hashlib.sha256(raw_manifest).hexdigest()))

    known_manifest_hashes = {digest for _, _, digest in documents}
    prior_links = [
        document["prior_manifest_sha256"]
        for _, document, _ in documents
        if document.get("prior_manifest_sha256") is not None
    ]
    if len(prior_links) != 1 or prior_links[0] not in known_manifest_hashes:
        pytest.fail("remaining-round manifest does not pin the prior manifest hash")

    completed: dict[str, dict[str, Any]] = {}
    superseded_failures: dict[str, dict[str, Any]] = {}
    for manifest_path, document, _ in documents:
        manifest_parent = manifest_path.parent.resolve()
        for entry in document["requests"]:
            if not isinstance(entry, dict):
                pytest.fail("capture manifest request entry is not an object")
            case_id = entry.get("case_id")
            statement_type = entry.get("statement_type")
            periodicity = entry.get("periodicity")
            if (
                not isinstance(case_id, str)
                or statement_type not in _STATEMENT_ENDPOINTS
                or periodicity not in _PERIODICITIES
            ):
                pytest.fail("capture manifest request identity is invalid")
            if case_id != f"{statement_type}-{periodicity}":
                pytest.fail("capture manifest case_id does not match its scope")
            if entry.get("endpoint") != _STATEMENT_ENDPOINTS[statement_type]:
                pytest.fail("capture manifest endpoint does not match its statement")

            status = entry.get("status")
            if status != "completed":
                if status in {"failed", "not_attempted"}:
                    superseded_failures.setdefault(case_id, entry)
                else:
                    pytest.fail("capture manifest has an unknown request status")
                continue

            if case_id in completed:
                pytest.fail("capture manifest repeats a completed scope")
            params = entry.get("request_params")
            if (
                not isinstance(params, dict)
                or set(params) != {"limit", "period", "thscode"}
                or params.get("thscode") != _INSTRUMENT
                or params.get("period") != periodicity
                or params.get("limit") != 1
            ):
                pytest.fail("capture manifest request parameters do not match the scope")
            if entry.get("http_status") != 200:
                pytest.fail("capture manifest completed response is not HTTP 200")
            received_at = _utc_datetime(entry.get("received_at_utc"), "received_at_utc")

            filename = entry.get("entity_filename")
            entity_sha256 = entry.get("entity_sha256")
            entity_bytes = entry.get("entity_bytes")
            if (
                not isinstance(filename, str)
                or not filename
                or filename in {".", ".."}
                or "/" in filename
                or "\\" in filename
            ):
                pytest.fail("capture manifest entity filename is not a simple file name")
            if (
                not isinstance(entity_sha256, str)
                or len(entity_sha256) != 64
                or any(character not in "0123456789abcdef" for character in entity_sha256)
            ):
                pytest.fail("capture manifest entity SHA-256 is malformed")
            if type(entity_bytes) is not int:
                pytest.fail("capture manifest entity size is malformed")

            entity_path = manifest_parent / filename
            if entity_path.parent.resolve() != manifest_parent:
                pytest.fail("capture entity resolves outside its manifest directory")
            body = _read_regular_entity(entity_path, entity_bytes)
            if hashlib.sha256(body).hexdigest() != entity_sha256:
                pytest.fail("capture entity SHA-256 does not match its manifest")

            expected = _expected_values(
                body,
                statement_type=statement_type,
                periodicity=periodicity,
            )
            completed[case_id] = {
                "case_id": case_id,
                "statement_type": statement_type,
                "periodicity": periodicity,
                "received_at": received_at,
                "received_at_utc": _utc_iso_z(received_at),
                "entity_sha256": entity_sha256,
                "entity_bytes": entity_bytes,
                "body": body,
                **expected,
            }

    expected_cases = {
        f"{statement_type}-{periodicity}"
        for statement_type in _STATEMENTS
        for periodicity in _PERIODICITIES
    }
    if set(completed) != expected_cases:
        missing = sorted(expected_cases - set(completed))
        extra = sorted(set(completed) - expected_cases)
        pytest.fail(f"completed capture scopes do not match the six expected: {missing=} {extra=}")
    if len({case["entity_sha256"] for case in completed.values()}) != len(expected_cases):
        pytest.fail("capture manifests reuse an entity digest across scopes")
    if "balance-quarterly" in superseded_failures:
        failure = superseded_failures["balance-quarterly"]
        if failure.get("failure_code") != "transport_failed":
            pytest.fail("historical balance-quarterly failure code changed")
    return [
        completed[f"{statement}-{periodicity}"]
        for statement in _STATEMENTS
        for periodicity in _PERIODICITIES
    ]


def _write_replay_bundle(path: Path) -> str:
    """Write one pinned 600519.SH bundle with explicitly unverified CNY units."""
    from opendata.data.instrument_reference import (
        InstrumentLifecycle,
        InstrumentReferenceRegistry,
        LifecycleStatus,
    )
    from opendata.data.instrument_reference_snapshot import (
        encode_instrument_reference_snapshot,
    )
    from opendata.data.providers.ths.endpoints import financial_statement_items
    from opendata.data.providers.ths.models.financial_observation_fetcher import (
        ThsFinancialNativeUnitProfile,
        canonical_native_unit_mapping_sha256,
    )

    registry = InstrumentReferenceRegistry(
        registry_version="fuyao-six-offline-replay-registry-v1",
        evidence_ref="offline-replay:test-only-registry-not-source-evidence",
        lifecycles=(
            InstrumentLifecycle(
                dataset_id="financial_statement",
                source_id="ths",
                instrument_id=_INSTRUMENT,
                lifecycle_version="fuyao-six-offline-replay-lifecycle-v1",
                status=LifecycleStatus.ACTIVE,
                status_as_of=date(2026, 10, 28),
                status_evidence_ref="offline-replay:test-only-status-not-source-evidence",
                start_boundary_unknown=True,
            ),
        ),
    )
    profiles: dict[str, Any] = {}
    for statement in _STATEMENTS:
        native_units = dict.fromkeys(financial_statement_items(statement), "CNY")
        version = "fuyao-six-offline-replay-unit-v1"
        profiles[statement] = ThsFinancialNativeUnitProfile(
            statement_type=statement,
            version=version,
            native_units=native_units,
            mapping_sha256_declared=canonical_native_unit_mapping_sha256(
                statement_type=statement,
                version=version,
                native_units=native_units,
            ),
            evidence_ref="offline-replay:operator-declared-unit-not-verified",
        )
    document = {
        "schema_version": 1,
        "scope": {"instrument_id": _INSTRUMENT},
        "instrument_reference_snapshot": json.loads(
            encode_instrument_reference_snapshot(registry).decode("utf-8")
        ),
        "unit_profiles": {
            statement: {
                "statement_type": profile.statement_type,
                "version": profile.version,
                "native_units": dict(profile.native_units),
                "mapping_sha256_declared": profile.mapping_sha256_declared,
                "evidence_ref": profile.evidence_ref,
            }
            for statement, profile in profiles.items()
        },
    }
    encoded = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    path.write_bytes(encoded)
    path.chmod(0o600)
    return hashlib.sha256(encoded).hexdigest()


def test_six_captured_fuyao_responses_replay_offline_to_afi_reader(
    hermetic_replay_environment: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """Replay all six captured scopes into one archive and read them with AFI."""
    if os.environ.get("FUYAO_SIX_REPLAY") != "1":
        pytest.skip(
            "six-sample Fuyao replay is opt-in; set FUYAO_SIX_REPLAY=1 with "
            "FUYAO_SIX_REPLAY_SAMPLE_DIR and AFI_CHECKOUT_SNAPSHOT"
        )
    sample_value = os.environ.get("FUYAO_SIX_REPLAY_SAMPLE_DIR", "").strip()
    if not sample_value:
        pytest.fail("FUYAO_SIX_REPLAY_SAMPLE_DIR must point to the private Fuyao capture root")
    afi_snapshot_value = os.environ.get("AFI_CHECKOUT_SNAPSHOT", "").strip()
    if not afi_snapshot_value:
        pytest.fail("AFI_CHECKOUT_SNAPSHOT must point to an ai-for-investor checkout snapshot")
    afi_snapshot = Path(afi_snapshot_value)
    if not afi_snapshot.is_absolute():
        pytest.fail("AFI_CHECKOUT_SNAPSHOT must be an absolute path")
    backend_root = afi_snapshot / "src/backend"
    if not backend_root.is_dir():
        pytest.fail("AFI_CHECKOUT_SNAPSHOT must contain src/backend")

    # The test itself is skipped before this point unless FUYAO_SIX_REPLAY=1.
    cases = _load_cases(Path(sample_value))
    support = _load_existing_job_test_support()

    from opendata.data.providers.ths.models import financial_observation_fetcher
    from opendata.data.source_attempt_gate import SourceAttemptGate
    from opendata.pipeline import fuyao_financial_process as process_module
    from opendata.pipeline.financial_observation_store import CAPTURES, OBSERVATIONS
    from opendata.pipeline.fuyao_financial_run_job import (
        build_fuyao_financial_job,
        build_fuyao_financial_manual_payload,
    )
    from opendata.pipeline.process_executor import (
        FUYAO_PROCESS_API_KEY_ENV,
        FUYAO_PROCESS_BINDING_ENV,
        FuyaoProcessDeploymentBinding,
        ProcessExecutionResult,
        ReviewedProcessExecutor,
    )
    from opendata.pipeline.run_identity import (
        RunRequest,
        ScheduleMetadata,
        TaskDefinition,
    )
    from opendata.pipeline.run_service import RunExecutionContext

    gate_engine = support._gate_engine(hermetic_replay_environment / "source-gate.sqlite")
    request.addfinalizer(gate_engine.dispose)
    bundle_path = hermetic_replay_environment / "financial-bundle.json"
    bundle_sha256 = _write_replay_bundle(bundle_path)
    policy_path = hermetic_replay_environment / "source-policy.json"
    policy_sha256 = support._write_pinned_policy(policy_path)
    binding = FuyaoProcessDeploymentBinding(
        api_key=support._SYNTHETIC_API_KEY,
        financial_bundle_path=str(bundle_path),
        financial_bundle_sha256=bundle_sha256,
        source_policy_path=str(policy_path),
        source_policy_sha256=policy_sha256,
        gate_database_path=str(gate_engine.url.database),
        source_attempt_grant_id=support._GRANT_ID,
        source_attempt_expected_revision=1,
        source_attempt_principal_scope=support._PRINCIPAL_SCOPE,
        raw_artifact_root=str(hermetic_replay_environment / "raw-evidence"),
        retention_class="restricted",
        source_history_limit="unknown",
    )

    warehouse_path = hermetic_replay_environment / "test_financial_archive_six_scope.sqlite"
    warehouse = sa.create_engine(
        f"sqlite:///{warehouse_path}",
        connect_args={"timeout": 10},
        poolclass=NullPool,
    )
    request.addfinalizer(warehouse.dispose)

    @event.listens_for(warehouse, "connect")
    def enable_foreign_keys(dbapi_connection: Any, _: object) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    support._migrate(
        warehouse,
        support._ARCHIVE_MIGRATION,
        "fuyao_six_replay_archive_migration_test",
    )

    disabled_task = TaskDefinition(
        job_id="fuyao-financial-observation",
        version="process-v1",
        dataset_id="financial_observation_archive",
        provider="ths",
        model="financial_statement",
        enabled=False,
    )
    preparation_job = build_fuyao_financial_job(disabled_task, deployment_binding=binding)
    active_task = TaskDefinition(
        job_id=disabled_task.job_id,
        version=disabled_task.version,
        dataset_id=disabled_task.dataset_id,
        provider=disabled_task.provider,
        model=disabled_task.model,
        enabled=True,
        schedule=ScheduleMetadata(
            kind="interval",
            expression="PT1H",
            description="Offline replay only; no scheduler is registered.",
        ),
    )
    job = replace(preparation_job, task=active_task, reviewed=True)

    monkeypatch.setenv(FUYAO_PROCESS_API_KEY_ENV, support._SYNTHETIC_API_KEY)
    monkeypatch.setenv(FUYAO_PROCESS_BINDING_ENV, support._binding_document(binding))

    def execute_replay_child(
        _executor: ReviewedProcessExecutor,
        _binding: object,
        parameters: Any,
        *,
        timeout_seconds: float,
        checkpoint: Any = None,
        cancelled: Any = None,
    ) -> ProcessExecutionResult:
        """Invoke the fixed child handler in-process; executor IPC is out of scope."""
        if timeout_seconds <= 0 or cancelled is not None:
            raise AssertionError("replayed child boundary received invalid controls")
        if checkpoint is not None:
            checkpoint()
        value = process_module.execute_fuyao_financial_request(parameters)
        return ProcessExecutionResult(
            value=value,
            stdout_discarded_bytes=0,
            stderr_discarded_bytes=0,
            stdout_truncated=False,
            stderr_truncated=False,
        )

    monkeypatch.setattr(ReviewedProcessExecutor, "run_fuyao", execute_replay_child)

    for case in sorted(cases, key=lambda item: item["received_at"]):
        sent_requests = support._install_mock_process_fetcher(monkeypatch, case["body"])
        received_at = case["received_at"]
        monkeypatch.setattr(
            financial_observation_fetcher,
            "_utc_now",
            lambda received_at=received_at: received_at,
        )
        # The local gate would otherwise stamp "now", which is later than the
        # captured receive time and would make the replay's observation time
        # inconsistent. Keep the gate clock one second before that capture.
        replay_reserved_at_us = int(received_at.timestamp() * 1_000_000) - 1_000_000
        monkeypatch.setattr(
            SourceAttemptGate,
            "_database_now_us",
            lambda self, connection, value=replay_reserved_at_us: value,
        )
        run_key = hashlib.sha256(
            f"fuyao-six-offline-replay:{case['case_id']}".encode("ascii")
        ).hexdigest()
        run_request = RunRequest(
            run_type="manual",
            **build_fuyao_financial_manual_payload(
                _INSTRUMENT,
                statement_type=case["statement_type"],
                periodicity=case["periodicity"],
                limit=1,
                idempotency_key=f"fuyao-six-offline-replay-{case['case_id']}",
            ),
        )
        context = RunExecutionContext(
            run_key=run_key,
            worker="fuyao-six-offline-replay-worker",
            fencing_token=1,
            _checkpoint=lambda: None,
        )

        raw_capture = job.fetch(run_request, context)
        if len(sent_requests) != 1:
            pytest.fail("one replay scope did not issue exactly one mocked request")
        sent = sent_requests[0]
        if (
            sent.url.params.get("thscode") != _INSTRUMENT
            or sent.url.params.get("period") != case["periodicity"]
            or sent.url.params.get("limit") != "1"
        ):
            pytest.fail("replayed request parameters do not match the captured scope")
        if (
            raw_capture.instrument_id != _INSTRUMENT
            or raw_capture.statement_type != case["statement_type"]
            or raw_capture.periodicity != case["periodicity"]
            or raw_capture.quarantined_row_count != 0
            or len(raw_capture.observation_ids) != len(case["items"])
            or len(set(raw_capture.observation_ids)) != len(case["items"])
        ):
            pytest.fail("replayed capture scope or observation count is inconsistent")

        batch = job.normalize(raw_capture, run_request, context)
        if batch.commit_payload is None:
            pytest.fail("replayed capture produced no commit payload")
        commit_decoder = job.decode_commit_payload
        if commit_decoder is None:
            pytest.fail("reviewed Fuyao job exposes no commit-payload decoder")
        prepared = commit_decoder(batch.commit_payload)
        publish_batch = replace(batch, commit_payload=None, commit_context=prepared)
        with warehouse.connect() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            publication = job.publish(
                connection,
                active_task,
                run_request,
                publish_batch,
                run_key,
            )
            connection.commit()
        if publication.record_count != len(case["items"]):
            pytest.fail("published observation count does not match the capture")

    with warehouse.connect() as connection:
        if connection.scalar(select(func.count()).select_from(CAPTURES)) != 6:
            pytest.fail("the archive does not hold exactly six capture records")
        if connection.scalar(select(func.count()).select_from(OBSERVATIONS)) != 52:
            pytest.fail("the archive does not hold the expected 52 observations")
        capture_rows = (
            connection.execute(
                select(
                    CAPTURES.c.instrument_id,
                    CAPTURES.c.statement_type,
                    CAPTURES.c.periodicity,
                    CAPTURES.c.observed_at_utc,
                    CAPTURES.c.http_status,
                    CAPTURES.c.raw_object_sha256,
                    CAPTURES.c.raw_object_size_bytes,
                )
            )
            .mappings()
            .all()
        )
    captures_by_scope = {(row["statement_type"], row["periodicity"]): row for row in capture_rows}
    if len(captures_by_scope) != 6:
        pytest.fail("the archive captures do not cover six distinct scopes")
    for case in cases:
        capture_row = captures_by_scope[(case["statement_type"], case["periodicity"])]
        if (
            capture_row["instrument_id"] != _INSTRUMENT
            or capture_row["http_status"] != 200
            or capture_row["raw_object_sha256"] != case["entity_sha256"]
            or capture_row["raw_object_size_bytes"] != case["entity_bytes"]
            or capture_row["observed_at_utc"] != case["received_at"].replace(tzinfo=None)
        ):
            pytest.fail(f"archived capture evidence for {case['case_id']} is inconsistent")

    from opendata.api import financial_observation_archive as archive_api
    from opendata.api.dependencies import get_current_principal
    from opendata.data.financial_observation_archive_reader import (
        FinancialObservationArchivePage,
        FinancialObservationArchiveRow,
    )

    def read_test_archive_page(
        *,
        source_id: str,
        instrument_id: str,
        statement_type: str,
        periodicity: str,
        start_report_period: date,
        end_report_period: date,
        page_size: int,
    ) -> dict[str, object]:
        """Use only SELECTs against the six-scope test archive in place of MySQL RR."""
        filters = and_(
            OBSERVATIONS.c.source_id == source_id,
            OBSERVATIONS.c.instrument_id == instrument_id,
            OBSERVATIONS.c.statement_type == statement_type,
            OBSERVATIONS.c.periodicity == periodicity,
            OBSERVATIONS.c.report_period >= start_report_period,
            OBSERVATIONS.c.report_period <= end_report_period,
        )
        with warehouse.connect() as connection:
            mappings = (
                connection.execute(
                    select(OBSERVATIONS)
                    .where(filters)
                    .order_by(OBSERVATIONS.c.report_period, OBSERVATIONS.c.item_code)
                    .limit(page_size + 1)
                )
                .mappings()
                .all()
            )
        if len(mappings) > page_size:
            pytest.fail("test archive page exceeded the requested page size")
        rows = tuple(
            FinancialObservationArchiveRow(
                observation_id=row["observation_id"],
                natural_key_sha256=row["natural_key_sha256"],
                content_sha256=row["content_sha256"],
                source_id=row["source_id"],
                instrument_id=row["instrument_id"],
                statement_type=row["statement_type"],
                periodicity=row["periodicity"],
                report_period=row["report_period"],
                source_disclosure_date=row["source_disclosure_date"],
                observed_at_utc=(
                    row["observed_at_utc"].replace(tzinfo=timezone.utc)
                    if row["observed_at_utc"].tzinfo is None
                    else row["observed_at_utc"]
                ),
                item_code=row["item_code"],
                value_decimal_text=row["value_decimal_text"],
                native_unit=row["native_unit"],
                source_metadata_json=row["source_metadata_json"],
                observation_policy=row["observation_policy"],
            )
            for row in mappings
        )
        page = FinancialObservationArchivePage(rows=rows, next_cursor=None)
        return archive_api._response_payload(
            page,
            source_id=source_id,
            instrument_id=instrument_id,
            statement_type=statement_type,
            periodicity=periodicity,
            start_report_period=start_report_period,
            end_report_period=end_report_period,
        )

    monkeypatch.setattr(archive_api, "_read_archive_page", read_test_archive_page)
    app = FastAPI()
    app.include_router(archive_api.router, prefix="/api/v2/data")
    app.dependency_overrides[get_current_principal] = lambda: SimpleNamespace(
        allows_domain=lambda domain: domain == "financial_statement"
    )

    credentials_type, reader_type = _load_afi_reader_types(backend_root)
    replay_api_key = "offline-six-replay-opendata-contract-key"
    api_requests: list[httpx.Request] = []
    route_payloads: list[dict[str, Any]] = []
    with TestClient(app, base_url="https://opendata.example.test") as api_client:

        async def call_local_opendata(request: httpx.Request) -> httpx.Response:
            api_requests.append(request)
            if request.url.path != "/api/v2/data/financial-statements/archive":
                pytest.fail("AFI reader requested an unexpected route")
            if request.headers["X-API-Key"] != replay_api_key:
                pytest.fail("AFI reader did not send the synthetic replay API key")
            response = await asyncio.to_thread(
                api_client.get,
                request.url.path,
                params=list(request.url.params.multi_items()),
                headers={"X-API-Key": replay_api_key},
            )
            route_payloads.append(json.loads(response.content))
            return httpx.Response(
                response.status_code,
                headers=dict(response.headers),
                content=response.content,
                request=request,
            )

        async def consume_with_afi_reader() -> dict[str, Any]:
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(call_local_opendata),
                trust_env=False,
                follow_redirects=False,
            ) as client:
                reader = reader_type(
                    credentials_type(
                        replay_api_key,
                        base_url="https://opendata.example.test",
                    ),
                    client=client,
                    allowed_hosts={"opendata.example.test"},
                )
                return await reader.fetch_analysis_evidence(
                    instrument_id=_INSTRUMENT,
                    analysis_date=date(2026, 10, 29),
                )

        evidence = asyncio.run(consume_with_afi_reader())

    expected_scopes = {
        (statement_type, periodicity)
        for statement_type in _STATEMENTS
        for periodicity in _PERIODICITIES
    }
    if len(api_requests) != 6:
        pytest.fail("AFI reader did not issue exactly six archive queries")
    requested_scopes = [
        (request.url.params["statement_type"], request.url.params["periodicity"])
        for request in api_requests
    ]
    if set(requested_scopes) != expected_scopes or len(set(requested_scopes)) != 6:
        pytest.fail("AFI reader requested a substituted, missing, or duplicate scope")
    if {request.url.params["source_id"] for request in api_requests} != {_SOURCE_ID}:
        pytest.fail("AFI reader used an unexpected source id")
    if {request.url.params["instrument_id"] for request in api_requests} != {_INSTRUMENT}:
        pytest.fail("AFI reader used an unexpected instrument id")
    if {request.url.params["start_report_period"] for request in api_requests} != {"2024-01-01"}:
        pytest.fail("AFI reader used an unexpected report-period start")
    if {request.url.params["end_report_period"] for request in api_requests} != {"2026-10-29"}:
        pytest.fail("AFI reader used an unexpected report-period end")
    if any(request.url.params["page_size"] != "199" for request in api_requests):
        pytest.fail("AFI reader used an unexpected page size")
    if any(replay_api_key in str(request.url) for request in api_requests):
        pytest.fail("the replay credential leaked into a request URL")

    if len(route_payloads) != 6:
        pytest.fail("the archive route did not return exactly six payloads")
    payloads_by_scope: dict[tuple[str, str], dict[str, Any]] = {}
    for payload in route_payloads:
        scope = payload.get("scope")
        if not isinstance(scope, dict):
            pytest.fail("archive route payload has no scope")
        key = (scope.get("statement_type"), scope.get("periodicity"))
        if key in payloads_by_scope:
            pytest.fail("archive route returned a duplicate scope")
        if (
            payload.get("completeness") != "unknown"
            or payload.get("point_in_time_reconstruction") != "unavailable"
            or payload.get("period_amount_semantics") != "unknown"
        ):
            pytest.fail("archive route did not mark completeness/PIT/semantics unestablished")
        payloads_by_scope[key] = payload
    if set(payloads_by_scope) != expected_scopes:
        pytest.fail("archive route payload scopes do not match the six expected scopes")

    if evidence["status"] != "observed":
        pytest.fail("AFI reader did not observe the six archived scopes")
    if evidence["completeness"] != "unknown":
        pytest.fail("AFI evidence claims established completeness")
    if evidence["point_in_time_reconstruction"] != "unavailable":
        pytest.fail("AFI evidence claims point-in-time reconstruction")
    if evidence["period_amount_semantics"] != "unknown":
        pytest.fail("AFI evidence claims reviewed period-amount semantics")
    if evidence["eligible_for_decision_or_backtest"] is not False:
        pytest.fail("AFI evidence is unexpectedly eligible for decisions or backtests")
    if evidence["provenance_status"] != "incomplete":
        pytest.fail("AFI evidence claims complete provenance")
    if evidence["historical_as_of_status"] != "unavailable":
        pytest.fail("AFI evidence claims historical as-of support")
    if evidence["pagination_supported"] is not False:
        pytest.fail("AFI evidence unexpectedly claims pagination support")
    if evidence["future_disclosure_record_count"] != 0:
        pytest.fail("replayed disclosures unexpectedly post-date the analysis date")

    queries_by_scope = {
        (query["statement_type"], query["periodicity"]): query for query in evidence["queries"]
    }
    if set(queries_by_scope) != expected_scopes or len(evidence["queries"]) != 6:
        pytest.fail("AFI evidence queries do not cover exactly the six scopes")

    records_by_scope: dict[tuple[str, str], list[dict[str, Any]]] = {
        scope: [] for scope in expected_scopes
    }
    for record in evidence["records"]:
        records_by_scope[(record["statement_type"], record["periodicity"])].append(record)
    if len(evidence["records"]) != 52:
        pytest.fail("AFI reader returned an unexpected total record count")

    per_scope_counts: dict[str, int] = {}
    for case in cases:
        scope = (case["statement_type"], case["periodicity"])
        records = records_by_scope[scope]
        if len(records) != len(case["items"]):
            pytest.fail(f"scope {case['case_id']} returned an unexpected record count")
        if queries_by_scope[scope]["status"] != "ok":
            pytest.fail(f"scope {case['case_id']} was not read successfully")
        if queries_by_scope[scope]["record_count"] != len(case["items"]):
            pytest.fail(f"scope {case['case_id']} query count is inconsistent")
        if len({record["item_code"] for record in records}) != len(case["items"]):
            pytest.fail(f"scope {case['case_id']} contains duplicate or substituted items")
        if {record["item_code"] for record in records} != set(case["items"]):
            pytest.fail(f"scope {case['case_id']} item codes do not match the capture")
        payload_records = payloads_by_scope[scope].get("records")
        if not isinstance(payload_records, list) or len(payload_records) != len(case["items"]):
            pytest.fail(f"route payload for {case['case_id']} has an unexpected row count")

        for record in records:
            if record["source_id"] != _SOURCE_ID or record["instrument_id"] != _INSTRUMENT:
                pytest.fail(f"scope {case['case_id']} record source or instrument changed")
            if record["report_period"] != case["report_period"]:
                pytest.fail(f"scope {case['case_id']} report period was substituted")
            if record["source_disclosure_date"] != case["disclosure_date"]:
                pytest.fail(f"scope {case['case_id']} disclosure date was substituted")
            if record["observed_at_utc"] != case["received_at_utc"]:
                pytest.fail(f"scope {case['case_id']} observation time was not the manifest time")
            if record["native_unit"] != "CNY":
                pytest.fail(f"scope {case['case_id']} native unit was not preserved")
            expected_decimal = case["values"][record["item_code"]]
            if record["value_decimal_text"] != expected_decimal:
                pytest.fail(f"scope {case['case_id']} decimal text was not preserved exactly")
            if Decimal(record["value_decimal_text"]) != Decimal(expected_decimal):
                pytest.fail(f"scope {case['case_id']} decimal value changed")
        per_scope_counts[case["case_id"]] = len(records)

    print(
        "FUYAO_SIX_REPLAY_EVIDENCE="
        + json.dumps(
            {
                "per_scope_record_count": per_scope_counts,
                "total_records": len(evidence["records"]),
                "entity_sha256": {case["case_id"]: case["entity_sha256"] for case in cases},
            },
            sort_keys=True,
        )
    )
