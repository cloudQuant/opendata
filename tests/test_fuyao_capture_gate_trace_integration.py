"""Offline end-to-end binding from one SQLite gate reservation to one capture.

The gate, transport, response, and artifact root are all test-owned. A matched
trace is only a local equality check; neither the test nor these declarations
authenticate source authorization, a Fuyao receipt, or immutable audit history.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx
import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.pool import NullPool

from opendata.data.instrument_reference import (
    InstrumentLifecycle,
    InstrumentReferenceRegistry,
    LifecycleStatus,
)
from opendata.data.providers.ths.endpoints import (
    financial_statement_items,
    shanghai_midnight_millis,
)
from opendata.data.providers.ths.models.financial_observation_fetcher import (
    ThsFinancialNativeUnitProfile,
    ThsFinancialObservationFetcher,
    canonical_native_unit_mapping_sha256,
)
from opendata.data.providers.ths.transport.credentials import (
    DEFAULT_FUYAO_API_BASE_URL,
    FuyaoCredentials,
)
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestGrant,
    RequestOperation,
    SourceAttemptBinding,
    request_execution_scope,
)
from opendata.data.source_attempt_gate import (
    AttemptReservation,
    ReservationStatus,
    SourceAttemptGate,
    TransportAttempt,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from opendata.pipeline.fuyao_observation_capture import FuyaoSourceAttemptDeclaration


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = ROOT / "alembic_data/versions/20261027-0015_source_attempt_gate.py"
MIGRATION_NAME = "fuyao_capture_gate_trace_migration_0015_test"
SOURCE = "ths"
MODEL = "financial_statement"
PRINCIPAL = "offline-capture-worker"
GRANT_ID = "offline-capture-grant"
QUOTA_KEY = "offline-capture-quota"
INSTRUMENT_ID = "600519.SH"
MICROTOKEN = 1_000_000
FAR_FUTURE_US = 253_402_300_799_999_999
_SYNTHETIC_API_KEY_PLACEHOLDER = "offline-test-only-placeholder"


def _load_gate_migration() -> Any:
    """Load the SQLite-only attempt-gate migration for this fixture."""
    spec = importlib.util.spec_from_file_location(MIGRATION_NAME, MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def gate_engine(tmp_path: Path) -> Iterator[Any]:
    """Apply the gate schema to a fresh, test-owned file-backed SQLite database."""
    engine = sa.create_engine(
        f"sqlite:///{tmp_path / 'fuyao-capture-gate.sqlite'}",
        connect_args={"timeout": 10},
        poolclass=NullPool,
    )
    migration = _load_gate_migration()
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()
    try:
        yield engine
    finally:
        engine.dispose()


def _seed_synthetic_gate(engine: Any) -> None:
    """Add a clearly synthetic policy, source binding, and one test-only grant."""
    digest = hashlib.sha256(b"offline test fixture; no source authorization").hexdigest()
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO od_source_gate_policies "
                "(quota_key, policy_revision, rate_microtokens_per_second, "
                "capacity_microtokens, policy_reference, policy_sha256, configured_by, "
                "configured_at_us) VALUES (:quota, 1, :rate, :capacity, :reference, "
                ":digest, :actor, 1)"
            ),
            {
                "quota": QUOTA_KEY,
                "rate": MICROTOKEN,
                "capacity": 5 * MICROTOKEN,
                "reference": "synthetic:test-policy-only",
                "digest": digest,
                "actor": "offline-test",
            },
        )
        connection.execute(
            sa.text(
                "INSERT INTO od_source_gate_source_bindings "
                "(source, quota_key, binding_reference, binding_sha256, configured_by, "
                "configured_at_us) VALUES (:source, :quota, :reference, :digest, :actor, 1)"
            ),
            {
                "source": SOURCE,
                "quota": QUOTA_KEY,
                "reference": "synthetic:test-source-binding-only",
                "digest": digest,
                "actor": "offline-test",
            },
        )
        connection.execute(
            sa.text(
                "INSERT INTO od_source_gate_state "
                "(quota_key, tokens_micro, remainder, last_refill_us, policy_revision) "
                "VALUES (:quota, :tokens, 0, 1, 1)"
            ),
            {"quota": QUOTA_KEY, "tokens": 5 * MICROTOKEN},
        )
        connection.execute(
            sa.text(
                "INSERT INTO od_source_grants "
                "(grant_id, source, canonical_model, operation, principal_scope, decision, "
                "revision, not_before_us, expires_at_us, conditions_json, "
                "rights_evidence_reference, rights_evidence_sha256, operator_attestation_note, "
                "attested_by, attested_at_us, revoked_at_us, created_at_us, updated_at_us) "
                "VALUES (:grant, :source, :model, 'query', :principal, 'ALLOWED', 1, 1, "
                ":expires, '[]', :evidence, :digest, :note, 'offline-test', 1, NULL, 1, 1)"
            ),
            {
                "grant": GRANT_ID,
                "source": SOURCE,
                "model": MODEL,
                "principal": PRINCIPAL,
                "expires": FAR_FUTURE_US,
                "evidence": "synthetic:offline-fixture-only",
                "digest": digest,
                "note": "Synthetic fixture declaration; not legal authorization.",
            },
        )
        connection.execute(
            sa.text("INSERT INTO od_source_grant_hosts (grant_id, host) VALUES (:grant, :host)"),
            {"grant": GRANT_ID, "host": urlsplit(DEFAULT_FUYAO_API_BASE_URL).hostname},
        )
        connection.execute(
            sa.text(
                "INSERT INTO od_source_grant_host_sets "
                "(grant_id, revision, host_count, sealed_at_us, sealed_by, reason_code) "
                "VALUES (:grant, 1, 1, 1, 'offline-test', 'synthetic-test-only')"
            ),
            {"grant": GRANT_ID},
        )


def _profile() -> ThsFinancialNativeUnitProfile:
    """Declare only fixture units needed to run the income decoder offline."""
    units = dict.fromkeys(financial_statement_items("income"), "CNY")
    version = "synthetic-test-units-v1"
    digest = canonical_native_unit_mapping_sha256(
        statement_type="income",
        version=version,
        native_units=units,
    )
    return ThsFinancialNativeUnitProfile(
        statement_type="income",
        version=version,
        native_units=units,
        mapping_sha256_declared=digest,
        evidence_ref="synthetic-test-only",
    )


def _registry() -> InstrumentReferenceRegistry:
    """Supply one synthetic active registry row without loading external state."""
    return InstrumentReferenceRegistry(
        registry_version="synthetic-test-v1",
        evidence_ref="synthetic-test-only",
        lifecycles=(
            InstrumentLifecycle(
                dataset_id="financial_statement",
                source_id=SOURCE,
                instrument_id=INSTRUMENT_ID,
                lifecycle_version="synthetic-test-v1",
                status=LifecycleStatus.ACTIVE,
                status_as_of=date.today(),
                status_evidence_ref="synthetic-test-only",
                start_boundary_unknown=True,
            ),
        ),
    )


def _request_budget() -> RequestBudget:
    """Create a bounded request budget with a synthetic local allow declaration."""
    host = urlsplit(DEFAULT_FUYAO_API_BASE_URL).hostname
    assert host is not None
    grant = RequestGrant(
        source=SOURCE,
        canonical_model=MODEL,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="synthetic-test-reference-only",
        task_attempts=4,
        source_attempts=1,
        allowed_hosts={host},
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    return RequestBudget(task_attempts=4, source_attempts=1, grants=(grant,))


def _response_body() -> bytes:
    """Build one deterministic synthetic income response for the MockTransport."""
    period_end_ms = shanghai_midnight_millis(date(2024, 12, 31))
    disclosure_ms = shanghai_midnight_millis(date(2025, 3, 31))
    row: dict[str, object] = {
        "thscode": INSTRUMENT_ID,
        "ticker": "600519",
        "period": "annual",
        "fiscal_year": 2024,
        "fiscal_period": "FY",
        "period_end_ms": period_end_ms,
        "report_date_ms": disclosure_ms,
        "currency": "CNY",
    }
    row.update(dict.fromkeys(financial_statement_items("income"), "0"))
    row[financial_statement_items("income")[0]] = "123.45"
    return json.dumps(
        {
            "code": 0,
            "message": "ok",
            "request_id": "synthetic-provider-request-1",
            "data": {"timestamp": period_end_ms, "item": [row]},
        },
        separators=(",", ":"),
    ).encode("utf-8")


def _declaration_from_committed_gate_row(
    engine: Any,
    attempt_id: str,
) -> FuyaoSourceAttemptDeclaration:
    """Build bridge input from the exact committed reservation named by the trace."""
    from opendata.pipeline.fuyao_observation_capture import FuyaoSourceAttemptDeclaration

    with engine.connect() as connection:
        rows = (
            connection.execute(
                sa.text(
                    "SELECT * FROM od_source_attempt_reservations WHERE attempt_id = :attempt_id"
                ),
                {"attempt_id": attempt_id},
            )
            .mappings()
            .all()
        )
    assert len(rows) == 1
    row = rows[0]
    assert row["outcome"] == "RESERVED"
    attempt = TransportAttempt(
        attempt_id=row["attempt_id"],
        request_id=row["request_id"],
        attempt_no=row["attempt_no"],
        grant_id=row["grant_id"],
        expected_revision=row["expected_revision"],
        source=row["source"],
        canonical_model=row["canonical_model"],
        operation=row["operation"],
        principal_scope=row["principal_scope"],
        host=row["host"],
        run_key=row["run_key"],
    )
    reservation = AttemptReservation(
        status=ReservationStatus.RESERVED_NEW,
        reserved_at_us=row["reserved_at_us"],
    )
    return FuyaoSourceAttemptDeclaration(attempt=attempt, reservation=reservation)


def test_gate_fetch_trace_and_capture_match_one_offline_attempt(
    gate_engine: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Match one actual local reservation, fetch trace, and exact captured bytes."""
    # The pipeline package imports application settings during package setup.
    # Move into this test-owned empty directory before its first import so a
    # repository-root .env cannot enter the test process through that path.
    monkeypatch.chdir(tmp_path)
    from opendata.pipeline.fuyao_observation_capture import (
        FuyaoCaptureProvenance,
        prepare_fuyao_observation_capture,
    )
    from opendata.pipeline.raw_evidence import RawResponseArtifactWriter, RawResponseSource
    from opendata.pipeline.raw_evidence_reader import RawResponseArtifactReader

    _seed_synthetic_gate(gate_engine)
    gate = SourceAttemptGate(gate_engine)
    profile = _profile()
    body = _response_body()
    sent: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, content=body)

    mock_transport = httpx.MockTransport(respond)
    fetcher = ThsFinancialObservationFetcher(
        credentials=FuyaoCredentials(_SYNTHETIC_API_KEY_PLACEHOLDER),
        source_attempt_gate=gate,
        instrument_registry=_registry(),
        unit_profiles={
            "income": profile,
            "balance": ThsFinancialNativeUnitProfile(
                statement_type="balance",
                version="synthetic-test-units-v1",
                native_units=dict.fromkeys(financial_statement_items("balance"), "CNY"),
                mapping_sha256_declared=canonical_native_unit_mapping_sha256(
                    statement_type="balance",
                    version="synthetic-test-units-v1",
                    native_units=dict.fromkeys(financial_statement_items("balance"), "CNY"),
                ),
                evidence_ref="synthetic-test-only",
            ),
            "cashflow": ThsFinancialNativeUnitProfile(
                statement_type="cashflow",
                version="synthetic-test-units-v1",
                native_units=dict.fromkeys(financial_statement_items("cashflow"), "CNY"),
                mapping_sha256_declared=canonical_native_unit_mapping_sha256(
                    statement_type="cashflow",
                    version="synthetic-test-units-v1",
                    native_units=dict.fromkeys(financial_statement_items("cashflow"), "CNY"),
                ),
                evidence_ref="synthetic-test-only",
            ),
        },
        mock_transport=mock_transport,
    )
    request_budget = _request_budget()
    binding = SourceAttemptBinding(
        grant_id=GRANT_ID,
        expected_revision=1,
        principal_scope=PRINCIPAL,
        run_key="synthetic-run-1",
    )

    try:
        with request_execution_scope(
            source=SOURCE,
            canonical_model=MODEL,
            operation=RequestOperation.QUERY,
            budget=request_budget,
            source_attempt_binding=binding,
        ):
            fetch_result = fetcher.fetch(
                instrument_id=INSTRUMENT_ID,
                statement_type="income",
                periodicity="annual",
                limit=1,
            )
    finally:
        fetcher.close()

    assert len(sent) == 1
    assert len(fetch_result.attempt_trace) == 1
    trace = fetch_result.attempt_trace[-1]
    declaration = _declaration_from_committed_gate_row(gate_engine, trace.attempt_id)
    attempt = declaration.attempt
    reservation = declaration.reservation

    # These identifiers and the successful reservation timestamp came from the
    # migrated SQLite ledger, then matched the fetcher's local transport trace.
    assert trace.transport_request_id == attempt.request_id
    assert trace.attempt_id == attempt.attempt_id
    assert trace.attempt_no == attempt.attempt_no == 1
    assert trace.gate_status == reservation.status.value == "RESERVED_NEW"
    assert trace.reserved_at_us == reservation.reserved_at_us
    assert trace.http_status == fetch_result.status_code == 200
    assert trace.source_request_id == fetch_result.source_request_id

    provenance = FuyaoCaptureProvenance(
        raw_source=RawResponseSource(
            provider_id="ths",
            dataset_id="ths_financial_statement",
            operation_id="income_annual",
        ),
        retention_class="restricted",
        source_history_limit="unknown",
    )
    artifact_root = tmp_path / "synthetic-private-raw-evidence"
    outcome = prepare_fuyao_observation_capture(
        fetch_result,
        unit_profile=profile,
        provenance=provenance,
        source_attempt_declaration=declaration,
        writer=RawResponseArtifactWriter(artifact_root),
    )

    assert outcome.attempt_association_status == "local_trace_matched_unverified"
    assert outcome.source_request_id_match_status == "matched_unverified"
    assert outcome.attempt_trace == fetch_result.attempt_trace
    assert outcome.source_attempt_declaration.attempt.request_id == trace.transport_request_id
    assert outcome.source_attempt_declaration.attempt.attempt_id == trace.attempt_id
    assert fetch_result.body_sha256 == hashlib.sha256(fetch_result.raw_body).hexdigest()
    assert fetch_result.request_summary_sha256 is not None

    evidence = RawResponseArtifactReader(artifact_root).read(
        outcome.manifest_sha256,
        expected_source=provenance.raw_source,
        expected_request_summary_sha256=fetch_result.request_summary_sha256,
        captured_at_not_after=datetime.now(timezone.utc),
        expected_http_status=fetch_result.status_code,
    )
    assert evidence.body == fetch_result.raw_body == body
    assert evidence.manifest.response_sha256 == fetch_result.body_sha256
    assert evidence.manifest.request_summary_sha256 == fetch_result.request_summary_sha256
