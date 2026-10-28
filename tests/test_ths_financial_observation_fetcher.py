"""Offline behavior tests for the opt-in, bounded Fuyao observation fetcher.

All sends use HTTPX MockTransport. The durable source-attempt ledger is a
per-test SQLite file under ``tmp_path`` with synthetic, non-authorizing rows.
These synthetic fixtures cover local edge cases only; no accepted Fuyao
recording or live-source acceptance is represented. No environment credential,
application database or scheduler is used by these tests.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from operator import setitem
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

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
    FINANCIAL_PERIOD_END,
    FINANCIAL_STATEMENT_ENDPOINTS,
    financial_statement_items,
    shanghai_midnight_millis,
)
from opendata.data.providers.ths.models import financial_observation_fetcher as fetcher_module
from opendata.data.providers.ths.models.financial_observation_fetcher import (
    FinancialObservationFetchError,
    FinancialObservationFetchResult,
    ThsFinancialNativeUnitProfile,
    ThsFinancialObservationFetcher,
    canonical_native_unit_mapping_sha256,
)
from opendata.data.providers.ths.models.financial_observation_raw import (
    FinancialObservationDecodeError,
    FinancialObservationDecodeResult,
    QuarantinedFinancialObservationRow,
)
from opendata.data.providers.ths.transport.credentials import (
    DEFAULT_FUYAO_API_BASE_URL,
    FuyaoCredentials,
)
from opendata.data.providers.ths.transport.errors import FuyaoError
from opendata.data.providers.ths.transport.rate_limiter import FuyaoRateLimiter
from opendata.data.request_budget import (
    GrantDecision,
    RequestAttemptLimitError,
    RequestBudget,
    RequestBudgetProfile,
    RequestBudgetScopeError,
    RequestExecutionCancelledError,
    RequestExecutionScope,
    RequestGrant,
    RequestOperation,
    SourceAttemptBinding,
    request_execution_scope,
)
from opendata.data.request_summary import request_summary_sha256
from opendata.data.source_attempt_gate import SourceAttemptGate

if TYPE_CHECKING:
    from opendata.data.financial_observation import StatementType

ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = ROOT / "alembic_data/versions/20261027-0015_source_attempt_gate.py"
MIGRATION_NAME = "financial_observation_fetcher_migration_0015_test"
SOURCE = "ths"
MODEL = "financial_statement"
PRINCIPAL = "fixture-observation-fetcher"
GRANT_ID = "fixture-observation-grant"
QUOTA_KEY = "fixture-observation-quota-v1"
HOST = "fuyao.aicubes.cn"
NOW_US = 1_700_000_000_000_000
FAR_FUTURE_US = 253_402_300_799_999_999
UTC = timezone.utc
SHANGHAI = ZoneInfo("Asia/Shanghai")
REPORT_PERIOD = date(2024, 12, 31)
INSTRUMENT_ID = "600519.SH"


def _load_migration() -> Any:
    """Load the standalone gate migration for one disposable SQLite file."""
    spec = importlib.util.spec_from_file_location(MIGRATION_NAME, MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def gate_engine(tmp_path: Path) -> Any:
    """Create a private file-backed gate fixture and dispose it after one test."""
    engine = sa.create_engine(
        f"sqlite:///{tmp_path / 'financial-observation-gate.sqlite'}",
        connect_args={"timeout": 10},
        poolclass=NullPool,
    )
    migration = _load_migration()
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()
    try:
        yield engine
    finally:
        engine.dispose()


def _seed_gate(engine: Any) -> None:
    """Insert only synthetic gate policy, binding and grant rows."""
    digest = hashlib.sha256(b"offline test declaration, not source authorization").hexdigest()
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO od_source_gate_policies "
                "(quota_key, policy_revision, rate_microtokens_per_second, "
                "capacity_microtokens, policy_reference, policy_sha256, configured_by, "
                "configured_at_us) VALUES (:quota, 1, 1000000, 5000000, :reference, "
                ":digest, :actor, 1)"
            ),
            {
                "quota": QUOTA_KEY,
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
                "VALUES (:quota, 5000000, 0, :refill, 1)"
            ),
            {"quota": QUOTA_KEY, "refill": NOW_US},
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
                "evidence": "synthetic:offline-test-only",
                "digest": digest,
                "note": "Synthetic fixture; does not authorize source access.",
            },
        )
        connection.execute(
            sa.text("INSERT INTO od_source_grant_hosts (grant_id, host) VALUES (:grant, :host)"),
            {"grant": GRANT_ID, "host": HOST},
        )
        connection.execute(
            sa.text(
                "INSERT INTO od_source_grant_host_sets "
                "(grant_id, revision, host_count, sealed_at_us, sealed_by, reason_code) "
                "VALUES (:grant, 1, 1, 1, 'offline-test', 'synthetic-test-only')"
            ),
            {"grant": GRANT_ID},
        )


def _instrument_registry() -> InstrumentReferenceRegistry:
    """Build one explicit synthetic canonical identity and lifecycle snapshot."""
    lifecycle = InstrumentLifecycle(
        dataset_id="financial_statement",
        source_id="ths",
        instrument_id=INSTRUMENT_ID,
        lifecycle_version="fixture-lifecycle-v1",
        status=LifecycleStatus.ACTIVE,
        status_as_of=date(2025, 4, 18),
        status_evidence_ref="synthetic:test-only",
        known_start_date=date(2001, 8, 27),
        start_boundary_unknown=False,
        start_evidence_ref="synthetic:test-only",
    )
    return InstrumentReferenceRegistry(
        registry_version="fixture-registry-v1",
        evidence_ref="synthetic:test-only",
        lifecycles=(lifecycle,),
    )


def _unit_profiles(
    *, missing_item_for: str | None = None
) -> dict[str, ThsFinancialNativeUnitProfile]:
    """Build three synthetic unit declarations with computed canonical digests."""
    profiles: dict[str, ThsFinancialNativeUnitProfile] = {}
    statements: tuple[StatementType, ...] = ("income", "balance", "cashflow")
    for statement in statements:
        native_units = {
            item: "CNY/share" if item == "basic_eps" else "CNY"
            for item in financial_statement_items(statement)
        }
        if statement == missing_item_for:
            native_units.pop(next(iter(native_units)))
        version = "synthetic-operator-profile-v1"
        digest = canonical_native_unit_mapping_sha256(
            statement_type=statement,
            version=version,
            native_units=native_units,
        )
        profiles[statement] = ThsFinancialNativeUnitProfile(
            statement_type=statement,
            version=version,
            native_units=native_units,
            mapping_sha256_declared=digest,
            evidence_ref="synthetic:test-only-unit-declaration",
        )
    return profiles


def _financial_row(
    *,
    statement_type: str = "income",
    periodicity: str = "annual",
    report_period: date = REPORT_PERIOD,
    disclosure_date: date | None = None,
    include_disclosure: bool = True,
    instrument_id: str = INSTRUMENT_ID,
    values: dict[str, object] | None = None,
) -> dict[str, object]:
    """Create one synthetic strict-decoder row with explicit fiscal identity."""
    if periodicity == "annual":
        fiscal_period = "FY"
    else:
        fiscal_period = next(
            (
                period
                for period, month_day in FINANCIAL_PERIOD_END.items()
                if period != "FY" and month_day == (report_period.month, report_period.day)
            ),
            "",
        )
        if not fiscal_period:
            raise ValueError("quarterly fixture date must be a quarter end")
    row: dict[str, object] = {
        "thscode": instrument_id,
        "ticker": instrument_id.split(".", maxsplit=1)[0],
        "period": periodicity,
        "fiscal_year": report_period.year,
        "fiscal_period": fiscal_period,
        "period_end_ms": shanghai_midnight_millis(report_period),
        "currency": "CNY",
        **dict.fromkeys(financial_statement_items(statement_type), 0),
    }
    if include_disclosure:
        observed_disclosure_date = (
            disclosure_date if disclosure_date is not None else datetime.now(SHANGHAI).date()
        )
        row["report_date_ms"] = shanghai_midnight_millis(observed_disclosure_date)
    if values:
        row.update(values)
    return row


def _financial_body(
    *,
    request_id: str = "synthetic-financial-request-1",
    statement_type: str = "income",
    periodicity: str = "annual",
    report_period: date = REPORT_PERIOD,
    disclosure_date: date | None = None,
    include_disclosure: bool = True,
    rows: list[dict[str, object]] | None = None,
    values: dict[str, object] | None = None,
) -> bytes:
    """Encode synthetic source-shaped rows, preserving exact Decimal JSON tokens."""
    source_rows = rows or [
        _financial_row(
            statement_type=statement_type,
            periodicity=periodicity,
            report_period=report_period,
            disclosure_date=disclosure_date,
            include_disclosure=include_disclosure,
            values=values,
        )
    ]
    encoded_rows: list[dict[str, object]] = []
    decimal_tokens: list[tuple[str, str]] = []
    for row_index, source_row in enumerate(source_rows):
        encoded_row: dict[str, object] = {}
        for key, value in source_row.items():
            if type(value) is Decimal:
                marker = f"__decimal_row_{row_index}_field_{key}__"
                encoded_row[key] = marker
                decimal_tokens.append((json.dumps(marker), str(value)))
            else:
                encoded_row[key] = value
        encoded_rows.append(encoded_row)
    latest_timestamp = max(int(row["period_end_ms"]) for row in source_rows)
    body = json.dumps(
        {
            "code": 0,
            "message": "success",
            "request_id": request_id,
            "data": {"timestamp": latest_timestamp, "item": encoded_rows},
        },
        separators=(",", ":"),
    )
    for marker, token in decimal_tokens:
        body = body.replace(marker, token, 1)
    return body.encode("utf-8")


def _fetcher(
    body: bytes,
    *,
    gate: SourceAttemptGate,
    send_observer: Any = None,
    base_url: str = DEFAULT_FUYAO_API_BASE_URL,
    unit_profiles: dict[str, ThsFinancialNativeUnitProfile] | None = None,
    instrument_registry: InstrumentReferenceRegistry | None = None,
) -> tuple[ThsFinancialObservationFetcher, list[httpx.Request], FuyaoCredentials]:
    """Build a service-owned client with an exact in-process MockTransport."""
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if send_observer is not None:
            send_observer(request)
        return httpx.Response(
            200,
            content=body,
            headers={"content-type": "application/json; charset=utf-8"},
        )

    credentials = FuyaoCredentials("synthetic-key-not-a-credential", base_url=base_url)
    fetcher = ThsFinancialObservationFetcher(
        credentials=credentials,
        source_attempt_gate=gate,
        instrument_registry=instrument_registry or _instrument_registry(),
        unit_profiles=unit_profiles if unit_profiles is not None else _unit_profiles(),
        mock_transport=httpx.MockTransport(respond),
    )
    # The production default is process-wide per host. Give every fixture its
    # own generous bucket so unrelated tests cannot consume this case's tokens.
    fetcher._client._limiter = FuyaoRateLimiter(rate_per_second=1_000, burst=1_000)
    return fetcher, requests, credentials


def _attempt_rows(engine: Any) -> list[dict[str, object]]:
    """Read only the isolated fixture ledger to prove reserve-before-send."""
    with engine.connect() as connection:
        result = connection.execute(
            sa.text(
                "SELECT * FROM od_source_attempt_reservations "
                "ORDER BY request_id, attempt_no, attempt_id"
            )
        )
        return [dict(row) for row in result.mappings().all()]


def _request_budget(
    *,
    decision: GrantDecision = GrantDecision.ALLOWED,
    allowed_hosts: frozenset[str] = frozenset({HOST}),
    expires_at: datetime | None = None,
    conditions: tuple[str, ...] = (),
) -> RequestBudget:
    """Create a separate, finite in-memory request authorization for this fixture."""
    grant = RequestGrant(
        source=SOURCE,
        canonical_model=MODEL,
        operation=RequestOperation.QUERY,
        decision=decision,
        rights_evidence="synthetic:test-only",
        task_attempts=3,
        source_attempts=3,
        allowed_hosts=allowed_hosts,
        expires_at=expires_at or datetime.now(UTC) + timedelta(minutes=10),
        conditions=conditions,
        profile=RequestBudgetProfile.LIVE_SMOKE,
    )
    return RequestBudget(
        task_attempts=3,
        source_attempts=3,
        grants=(grant,),
        profile=RequestBudgetProfile.LIVE_SMOKE,
    )


def _scope(
    *,
    budget: RequestBudget | None = None,
    source: str = SOURCE,
    canonical_model: str | None = MODEL,
    operation: RequestOperation = RequestOperation.QUERY,
    binding: SourceAttemptBinding | None = None,
) -> RequestExecutionScope:
    """Build a synthetic scope directly for validation-boundary tests."""
    return RequestExecutionScope(
        source=source,
        canonical_model=canonical_model,
        operation=operation,
        budget=budget or _request_budget(),
        source_attempt_binding=_binding() if binding is None else binding,
    )


def _binding() -> SourceAttemptBinding:
    """Return the synthetic trusted binding carried by an active request scope."""
    return SourceAttemptBinding(
        grant_id=GRANT_ID,
        expected_revision=1,
        principal_scope=PRINCIPAL,
        run_key="fixture-observation-run",
    )


@pytest.fixture
def dependencies(gate_engine: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Yield only disposable gate and mock-transport dependencies for one test."""
    _seed_gate(gate_engine)
    gate = SourceAttemptGate(gate_engine)
    body = _financial_body()
    requests: list[httpx.Request] = []
    observed_times: list[datetime] = []

    def system_utc_after_send() -> datetime:
        """Test-only clock seam: prove receive precedes the one UTC sample."""
        assert len(requests) == 1
        observed_at = datetime.now(UTC)
        observed_times.append(observed_at)
        return observed_at

    monkeypatch.setattr(fetcher_module, "_utc_now", system_utc_after_send)
    fetcher, observed_requests, _credentials = _fetcher(
        body,
        gate=gate,
    )
    requests = observed_requests
    try:
        yield gate_engine, gate, requests, fetcher, body, observed_times
    finally:
        fetcher.close()


def test_fetch_uses_one_fixed_source_request_and_binds_result_to_raw_response(
    dependencies: Any,
) -> None:
    """Use canonical identity, bounded limit, a durable reservation and exact bytes."""
    engine, _gate, requests, fetcher, body, observed_times = dependencies
    with request_execution_scope(
        source=SOURCE,
        canonical_model=MODEL,
        operation=RequestOperation.QUERY,
        budget=_request_budget(),
        source_attempt_binding=_binding(),
    ):
        result = fetcher.fetch(
            instrument_id=INSTRUMENT_ID,
            statement_type="income",
            periodicity="annual",
            limit=5,
        )

    assert len(requests) == 1
    request = requests[0]
    assert request.method == "GET"
    assert request.url.scheme == "https"
    assert request.url.host == HOST
    assert request.url.path == FINANCIAL_STATEMENT_ENDPOINTS["income"]
    assert dict(request.url.params) == {
        "thscode": INSTRUMENT_ID,
        "period": "annual",
        "limit": "5",
    }
    assert result.request_summary_sha256 == (
        "89ff89c09c896b7107b658371b38755a3beb20eb03b1aee793f26bc5b381e412"
    )
    assert result.request_summary_sha256 == request_summary_sha256(
        {"thscode": INSTRUMENT_ID, "period": "annual", "limit": 5},
        allowed_fields={"thscode", "period", "start", "end", "limit"},
    )
    reservations = _attempt_rows(engine)
    assert len(reservations) == 1
    assert reservations[0]["outcome"] == "RESERVED"
    assert len(result.attempt_trace) == 1
    successful_attempt = result.attempt_trace[0]
    assert successful_attempt.attempt_no == 1
    assert successful_attempt.gate_status == "RESERVED_NEW"
    assert successful_attempt.request_budget_reserved is True
    assert successful_attempt.http_status == result.status_code == 200
    assert successful_attempt.outcome == "response_observed"
    assert successful_attempt.failure_category is None
    assert successful_attempt.source_request_id == result.source_request_id
    assert successful_attempt.transport_request_id != result.source_request_id
    assert result.raw_body == body
    assert result.body_sha256 == hashlib.sha256(body).hexdigest()
    assert result.source_request_id == "synthetic-financial-request-1"
    assert observed_times == [result.observed_at]
    assert result.unit_mapping_evidence_kind == "operator_declared"
    assert result.coverage_assessment == "not_assessed"
    assert result.usability_status == "USABLE_OBSERVATIONS_PRESENT"
    assert len(result.observations) == len(financial_statement_items("income"))
    assert result.quarantined_rows == ()
    assert all(item.body_sha256 == result.body_sha256 for item in result.observations)
    assert "raw_body" not in repr(result)
    assert "observations=" not in repr(result)
    assert "quarantined_rows=" not in repr(result)
    assert "attempt_trace=" not in repr(result)
    assert successful_attempt.transport_request_id not in repr(result)
    assert "synthetic-key-not-a-credential" not in repr(result)

    legacy_result = FinancialObservationFetchResult(
        instrument_id=result.instrument_id,
        statement_type=result.statement_type,
        periodicity=result.periodicity,
        source_request_id=result.source_request_id,
        body_sha256=result.body_sha256,
        observed_at=result.observed_at,
        status_code=result.status_code,
        unit_mapping_version_declared=result.unit_mapping_version_declared,
        unit_mapping_sha256_declared=result.unit_mapping_sha256_declared,
        unit_mapping_evidence_kind=result.unit_mapping_evidence_kind,
        coverage_assessment=result.coverage_assessment,
        usability_status=result.usability_status,
        raw_body=result.raw_body,
        observations=result.observations,
        quarantined_rows=result.quarantined_rows,
    )
    assert legacy_result.request_summary_sha256 is None
    assert legacy_result.attempt_trace == ()
    assert legacy_result == result


def test_request_summary_is_one_caller_digest_for_identical_transport_retries(
    gate_engine: Any,
) -> None:
    """A logical fetch retains one parameter digest across physical retries."""
    _seed_gate(gate_engine)
    gate = SourceAttemptGate(gate_engine)
    requests: list[httpx.Request] = []

    def timeout_first_attempt(request: httpx.Request) -> None:
        if len(requests) == 1:
            raise httpx.ConnectTimeout("synthetic offline timeout")

    fetcher, observed_requests, _credentials = _fetcher(
        _financial_body(),
        gate=gate,
        send_observer=timeout_first_attempt,
    )
    requests = observed_requests
    try:
        with request_execution_scope(
            source=SOURCE,
            canonical_model=MODEL,
            operation=RequestOperation.QUERY,
            budget=_request_budget(),
            source_attempt_binding=_binding(),
        ):
            result = fetcher.fetch(
                instrument_id=INSTRUMENT_ID,
                statement_type="income",
                periodicity="annual",
                limit=2,
            )

        assert len(requests) == 2
        assert requests[0].url.params == requests[1].url.params
        assert dict(requests[0].url.params) == {
            "thscode": INSTRUMENT_ID,
            "period": "annual",
            "limit": "2",
        }
        assert result.request_summary_sha256 == request_summary_sha256(
            {"thscode": INSTRUMENT_ID, "period": "annual", "limit": 2},
            allowed_fields={"thscode", "period", "start", "end", "limit"},
        )
        assert len(result.attempt_trace) == 2
        first, final = result.attempt_trace
        assert first.transport_request_id == final.transport_request_id
        assert first.attempt_id != final.attempt_id
        assert (first.attempt_no, final.attempt_no) == (1, 2)
        assert first.outcome == "outcome_unknown"
        assert first.http_status is None
        assert first.failure_category == "timeout"
        assert final.outcome == "response_observed"
        assert final.http_status == result.status_code == 200
        assert final.source_request_id == result.source_request_id
        assert len(_attempt_rows(gate_engine)) == 2
    finally:
        fetcher.close()


def test_terminal_transport_failure_has_no_fetch_result_trace(gate_engine: Any) -> None:
    """A terminal failure keeps its unknown outcome outside successful results."""
    _seed_gate(gate_engine)
    gate = SourceAttemptGate(gate_engine)

    def timeout_every_attempt(_request: httpx.Request) -> None:
        raise httpx.ConnectTimeout("synthetic terminal timeout")

    fetcher, requests, _credentials = _fetcher(
        _financial_body(),
        gate=gate,
        send_observer=timeout_every_attempt,
    )
    try:
        with (
            request_execution_scope(
                source=SOURCE,
                canonical_model=MODEL,
                operation=RequestOperation.QUERY,
                budget=_request_budget(),
                source_attempt_binding=_binding(),
            ),
            pytest.raises(FuyaoError) as error,
        ):
            fetcher.fetch(
                instrument_id=INSTRUMENT_ID,
                statement_type="income",
                periodicity="annual",
                limit=1,
            )

        assert len(requests) == fetcher._client._max_attempts == 3
        assert len(_attempt_rows(gate_engine)) == 3
        assert not hasattr(error.value, "attempt_trace")
    finally:
        fetcher.close()


def test_scope_and_registered_identity_fail_before_mock_transport_send(dependencies: Any) -> None:
    """Missing trusted scope and unknown/caller-selected identity send no request."""
    engine, _gate, requests, fetcher, _body, _observed_times = dependencies

    with pytest.raises(FinancialObservationFetchError, match="trusted_request_scope_required"):
        fetcher.fetch(
            instrument_id=INSTRUMENT_ID,
            statement_type="income",
            periodicity="annual",
            limit=1,
        )
    assert requests == []
    assert _attempt_rows(engine) == []

    with (
        request_execution_scope(
            source=SOURCE,
            canonical_model=MODEL,
            operation=RequestOperation.QUERY,
            budget=_request_budget(),
            source_attempt_binding=_binding(),
        ),
        pytest.raises(FinancialObservationFetchError, match="instrument_identity_unregistered"),
    ):
        fetcher.fetch(
            instrument_id="000001.SZ",
            statement_type="income",
            periodicity="annual",
            limit=1,
        )

    assert requests == []
    assert _attempt_rows(engine) == []


@pytest.mark.parametrize(
    ("query", "expected_code"),
    [
        ({"start_date": date(2010, 1, 1), "end_date": None}, "date_window_incomplete"),
        (
            {"start_date": date(2010, 1, 1), "end_date": date(2020, 1, 2)},
            "date_window_exceeds_ten_years",
        ),
        (
            {
                "start_date": date(2024, 1, 1),
                "end_date": date(2024, 1, 2),
                "limit": 1,
            },
            "exactly_one_query_bound_required",
        ),
        ({"limit": 21}, "period_limit_invalid"),
    ],
)
def test_query_bounds_fail_before_any_gate_reservation_or_send(
    dependencies: Any,
    query: dict[str, object],
    expected_code: str,
) -> None:
    """Reject unsupported request shape before consuming the durable send gate."""
    engine, _gate, requests, fetcher, _body, _observed_times = dependencies
    with (
        request_execution_scope(
            source=SOURCE,
            canonical_model=MODEL,
            operation=RequestOperation.QUERY,
            budget=_request_budget(),
            source_attempt_binding=_binding(),
        ),
        pytest.raises(FinancialObservationFetchError, match=expected_code),
    ):
        fetcher.fetch(
            instrument_id=INSTRUMENT_ID,
            statement_type="income",
            periodicity="annual",
            **query,
        )
    assert requests == []
    assert _attempt_rows(engine) == []


def test_unit_profile_hash_and_exact_item_coverage_fail_closed_before_send(
    gate_engine: Any,
) -> None:
    """Recompute the declared digest and refuse an incomplete selected statement map."""
    _seed_gate(gate_engine)
    gate = SourceAttemptGate(gate_engine)
    fetcher, requests, _credentials = _fetcher(
        _financial_body(),
        gate=gate,
        unit_profiles=_unit_profiles(missing_item_for="income"),
    )
    try:
        with pytest.raises(ValueError, match="digest does not match"):
            ThsFinancialNativeUnitProfile(
                statement_type="income",
                version="synthetic-operator-profile-v1",
                native_units={"net_profit": "CNY"},
                mapping_sha256_declared="0" * 64,
                evidence_ref="synthetic:test-only",
            )

        with (
            request_execution_scope(
                source=SOURCE,
                canonical_model=MODEL,
                operation=RequestOperation.QUERY,
                budget=_request_budget(),
                source_attempt_binding=_binding(),
            ),
            pytest.raises(FinancialObservationFetchError, match="unit_mapping_profile_mismatch"),
        ):
            fetcher.fetch(
                instrument_id=INSTRUMENT_ID,
                statement_type="income",
                periodicity="annual",
                limit=1,
            )
        assert requests == []
        assert _attempt_rows(gate_engine) == []
    finally:
        fetcher.close()


def test_credentials_must_use_fixed_fuyao_origin(gate_engine: Any) -> None:
    """Reject a caller-configured origin before creating the private transport."""
    _seed_gate(gate_engine)
    gate = SourceAttemptGate(gate_engine)
    with pytest.raises(FinancialObservationFetchError, match="fuyao_origin_not_allowed"):
        _fetcher(
            _financial_body(),
            gate=gate,
            base_url="https://caller-selected.invalid",
        )
    assert _attempt_rows(gate_engine) == []


@pytest.mark.parametrize("statement_type", ["income", "balance", "cashflow"])
def test_each_statement_supports_quarterly_request_and_registry_identity(
    gate_engine: Any,
    statement_type: str,
) -> None:
    """Exercise all three fixed endpoints with quarterly row identity and cadence."""
    _seed_gate(gate_engine)
    gate = SourceAttemptGate(gate_engine)
    quarter_end = date(2024, 3, 31)
    body = _financial_body(
        statement_type=statement_type,
        periodicity="quarterly",
        report_period=quarter_end,
        disclosure_date=date(2024, 4, 20),
    )
    fetcher, requests, _credentials = _fetcher(body, gate=gate)
    try:
        with request_execution_scope(
            source=SOURCE,
            canonical_model=MODEL,
            operation=RequestOperation.QUERY,
            budget=_request_budget(),
            source_attempt_binding=_binding(),
        ):
            result = fetcher.fetch(
                instrument_id=INSTRUMENT_ID,
                statement_type=statement_type,
                periodicity="quarterly",
                limit=1,
            )
        request = requests[0]
        assert request.url.path == FINANCIAL_STATEMENT_ENDPOINTS[statement_type]
        assert dict(request.url.params) == {
            "thscode": INSTRUMENT_ID,
            "period": "quarterly",
            "limit": "1",
        }
        assert result.request_summary_sha256 == request_summary_sha256(
            {"thscode": INSTRUMENT_ID, "period": "quarterly", "limit": 1},
            allowed_fields={"thscode", "period", "start", "end", "limit"},
        )
        assert result.statement_type == statement_type
        assert result.periodicity == "quarterly"
        assert result.observations[0].observation.report_period == quarter_end
        assert result.observations[0].row_pointer == "/data/item/0"
    finally:
        fetcher.close()


def test_window_parameters_and_validation_include_both_closed_endpoints(
    gate_engine: Any,
) -> None:
    """Accept a one-day report-period window and send both inclusive endpoints."""
    _seed_gate(gate_engine)
    gate = SourceAttemptGate(gate_engine)
    fetcher, requests, _credentials = _fetcher(_financial_body(), gate=gate)
    try:
        with request_execution_scope(
            source=SOURCE,
            canonical_model=MODEL,
            operation=RequestOperation.QUERY,
            budget=_request_budget(),
            source_attempt_binding=_binding(),
        ):
            result = fetcher.fetch(
                instrument_id=INSTRUMENT_ID,
                statement_type="income",
                periodicity="annual",
                start_date=REPORT_PERIOD,
                end_date=REPORT_PERIOD,
            )
        assert dict(requests[0].url.params) == {
            "thscode": INSTRUMENT_ID,
            "period": "annual",
            "start": str(shanghai_midnight_millis(REPORT_PERIOD)),
            "end": str(shanghai_midnight_millis(REPORT_PERIOD)),
        }
        assert result.request_summary_sha256 == (
            "4353794516213a5c4a2cadeac6d83676d9aea983a80884b6c3b0a2d6c3d4a90a"
        )
        assert result.request_summary_sha256 == request_summary_sha256(
            {
                "thscode": INSTRUMENT_ID,
                "period": "annual",
                "start": shanghai_midnight_millis(REPORT_PERIOD),
                "end": shanghai_midnight_millis(REPORT_PERIOD),
            },
            allowed_fields={"thscode", "period", "start", "end", "limit"},
        )
        assert result.observations[0].observation.report_period == REPORT_PERIOD
    finally:
        fetcher.close()


@pytest.mark.parametrize(
    ("report_period", "disclosure_date"),
    [
        (date(2023, 12, 31), date(2024, 4, 1)),
        (date(2025, 12, 31), None),
    ],
)
def test_every_response_row_must_be_inside_requested_window(
    gate_engine: Any,
    report_period: date,
    disclosure_date: date | None,
) -> None:
    """Reject lower- or upper-bound violations, including quarantined source rows."""
    _seed_gate(gate_engine)
    gate = SourceAttemptGate(gate_engine)
    body = _financial_body(
        report_period=report_period,
        disclosure_date=disclosure_date,
        include_disclosure=disclosure_date is not None,
    )
    fetcher, requests, _credentials = _fetcher(body, gate=gate)
    try:
        with (
            request_execution_scope(
                source=SOURCE,
                canonical_model=MODEL,
                operation=RequestOperation.QUERY,
                budget=_request_budget(),
                source_attempt_binding=_binding(),
            ),
            pytest.raises(
                FinancialObservationFetchError,
                match="response_report_period_outside_window",
            ),
        ):
            fetcher.fetch(
                instrument_id=INSTRUMENT_ID,
                statement_type="income",
                periodicity="annual",
                start_date=REPORT_PERIOD,
                end_date=REPORT_PERIOD,
            )
        assert len(requests) == 1
        assert len(_attempt_rows(gate_engine)) == 1
    finally:
        fetcher.close()


def test_limit_counts_rows_even_when_disclosure_quarantines_one(
    gate_engine: Any,
) -> None:
    """A quarantined row still counts against the source's requested period limit."""
    _seed_gate(gate_engine)
    gate = SourceAttemptGate(gate_engine)
    rows = [
        _financial_row(report_period=REPORT_PERIOD),
        _financial_row(
            report_period=date(2023, 12, 31),
            disclosure_date=None,
            include_disclosure=False,
        ),
    ]
    fetcher, requests, _credentials = _fetcher(_financial_body(rows=rows), gate=gate)
    try:
        with (
            request_execution_scope(
                source=SOURCE,
                canonical_model=MODEL,
                operation=RequestOperation.QUERY,
                budget=_request_budget(),
                source_attempt_binding=_binding(),
            ),
            pytest.raises(
                FinancialObservationFetchError,
                match="response_rows_exceed_requested_limit",
            ),
        ):
            fetcher.fetch(
                instrument_id=INSTRUMENT_ID,
                statement_type="income",
                periodicity="annual",
                limit=1,
            )
        assert len(requests) == 1
    finally:
        fetcher.close()


def test_all_quarantined_response_has_no_usable_observation_status(
    gate_engine: Any,
) -> None:
    """Expose an all-quarantined response explicitly without implying completeness."""
    _seed_gate(gate_engine)
    gate = SourceAttemptGate(gate_engine)
    body = _financial_body(disclosure_date=None, include_disclosure=False)
    fetcher, _requests, _credentials = _fetcher(body, gate=gate)
    try:
        with request_execution_scope(
            source=SOURCE,
            canonical_model=MODEL,
            operation=RequestOperation.QUERY,
            budget=_request_budget(),
            source_attempt_binding=_binding(),
        ):
            result = fetcher.fetch(
                instrument_id=INSTRUMENT_ID,
                statement_type="income",
                periodicity="annual",
                limit=1,
            )
        assert result.observations == ()
        assert len(result.quarantined_rows) == 1
        assert result.quarantined_rows[0].row_pointer == "/data/item/0"
        assert result.quarantined_rows[0].reason == "disclosure_date_missing"
        assert result.usability_status == "NO_USABLE_OBSERVATIONS"
        assert result.coverage_assessment == "not_assessed"
    finally:
        fetcher.close()


def test_mixed_response_is_partial_and_quarantined_pointer_is_preserved(
    gate_engine: Any,
) -> None:
    """Distinguish a partially usable response and retain the exact bad-row pointer."""
    _seed_gate(gate_engine)
    gate = SourceAttemptGate(gate_engine)
    rows = [
        _financial_row(report_period=REPORT_PERIOD),
        _financial_row(
            report_period=date(2023, 12, 31),
            disclosure_date=None,
            include_disclosure=False,
        ),
    ]
    fetcher, _requests, _credentials = _fetcher(_financial_body(rows=rows), gate=gate)
    try:
        with request_execution_scope(
            source=SOURCE,
            canonical_model=MODEL,
            operation=RequestOperation.QUERY,
            budget=_request_budget(),
            source_attempt_binding=_binding(),
        ):
            result = fetcher.fetch(
                instrument_id=INSTRUMENT_ID,
                statement_type="income",
                periodicity="annual",
                limit=2,
            )
        assert len(result.observations) == len(financial_statement_items("income"))
        assert len(result.quarantined_rows) == 1
        assert result.quarantined_rows[0].row_pointer == "/data/item/1"
        assert result.quarantined_rows[0].reason == "disclosure_date_missing"
        assert result.usability_status == "PARTIAL"
        assert result.coverage_assessment == "not_assessed"
    finally:
        fetcher.close()


def test_decimal_observation_keeps_exact_value_and_json_pointer(gate_engine: Any) -> None:
    """Preserve a fractional numeric token as Decimal and bind its exact source field."""
    _seed_gate(gate_engine)
    gate = SourceAttemptGate(gate_engine)
    item_code = financial_statement_items("income")[0]
    exact_value = Decimal("123456789012345678.123456789012345678")
    body = _financial_body(values={item_code: exact_value})
    fetcher, _requests, _credentials = _fetcher(body, gate=gate)
    try:
        with request_execution_scope(
            source=SOURCE,
            canonical_model=MODEL,
            operation=RequestOperation.QUERY,
            budget=_request_budget(),
            source_attempt_binding=_binding(),
        ):
            result = fetcher.fetch(
                instrument_id=INSTRUMENT_ID,
                statement_type="income",
                periodicity="annual",
                limit=1,
            )
        observation = next(
            reference.observation
            for reference in result.observations
            if reference.observation.item_code == item_code
        )
        reference = next(
            item for item in result.observations if item.observation.item_code == item_code
        )
        assert observation.value == exact_value
        assert isinstance(observation.value, Decimal)
        assert reference.row_pointer == "/data/item/0"
        assert reference.value_pointer == f"/data/item/0/{item_code}"
    finally:
        fetcher.close()


def test_source_row_identity_mismatch_is_rejected_with_row_pointer(gate_engine: Any) -> None:
    """A syntactically valid but unregistered source identity cannot be returned."""
    _seed_gate(gate_engine)
    gate = SourceAttemptGate(gate_engine)
    row = _financial_row(instrument_id="000001.SZ")
    fetcher, requests, _credentials = _fetcher(_financial_body(rows=[row]), gate=gate)
    try:
        with (
            request_execution_scope(
                source=SOURCE,
                canonical_model=MODEL,
                operation=RequestOperation.QUERY,
                budget=_request_budget(),
                source_attempt_binding=_binding(),
            ),
            pytest.raises(FinancialObservationDecodeError) as error,
        ):
            fetcher.fetch(
                instrument_id=INSTRUMENT_ID,
                statement_type="income",
                periodicity="annual",
                limit=1,
            )
        assert error.value.code == "row_identity_invalid"
        assert error.value.pointer == "/data/item/0"
        assert len(requests) == 1
    finally:
        fetcher.close()


def test_service_owned_client_ignores_later_mutation_of_caller_credentials(
    gate_engine: Any,
) -> None:
    """A caller cannot switch host or same-host path after constructing the service."""
    _seed_gate(gate_engine)
    gate = SourceAttemptGate(gate_engine)
    fetcher, requests, credentials = _fetcher(_financial_body(), gate=gate)
    credentials.base_url = "https://fuyao.aicubes.cn/caller-controlled-prefix"
    try:
        with request_execution_scope(
            source=SOURCE,
            canonical_model=MODEL,
            operation=RequestOperation.QUERY,
            budget=_request_budget(),
            source_attempt_binding=_binding(),
        ):
            fetcher.fetch(
                instrument_id=INSTRUMENT_ID,
                statement_type="income",
                periodicity="annual",
                limit=1,
            )
        assert requests[0].url.host == HOST
        assert requests[0].url.path == FINANCIAL_STATEMENT_ENDPOINTS["income"]
        assert "/caller-controlled-prefix" not in str(requests[0].url)
        assert "synthetic-key-not-a-credential" not in repr(credentials)
        assert "synthetic-key-not-a-credential" not in repr(fetcher)
    finally:
        fetcher.close()


def test_system_clock_samples_once_across_shanghai_midnight(
    dependencies: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Private test clock seam models midnight without exposing runtime backdating."""
    _engine, _gate, requests, fetcher, _body, _observed_times = dependencies
    shanghai_today = datetime.now(SHANGHAI).date()
    before_midnight = datetime(
        shanghai_today.year,
        shanghai_today.month,
        shanghai_today.day,
        23,
        59,
        59,
        tzinfo=SHANGHAI,
    ).astimezone(UTC)
    after_midnight = before_midnight + timedelta(seconds=2)
    clock_samples = iter((before_midnight, after_midnight))
    sampled_values: list[datetime] = []

    def synthetic_private_system_clock() -> datetime:
        """A private offline seam would cross Shanghai midnight on a second call."""
        value = next(clock_samples)
        sampled_values.append(value)
        return value

    # Tests only: production exposes no clock parameter and always calls the
    # system UTC helper directly after its synthetic MockTransport response.
    monkeypatch.setattr(fetcher_module, "_utc_now", synthetic_private_system_clock)
    with request_execution_scope(
        source=SOURCE,
        canonical_model=MODEL,
        operation=RequestOperation.QUERY,
        budget=_request_budget(),
        source_attempt_binding=_binding(),
    ):
        result = fetcher.fetch(
            instrument_id=INSTRUMENT_ID,
            statement_type="income",
            periodicity="annual",
            limit=5,
        )

    assert len(requests) == 1
    assert sampled_values == [before_midnight]
    assert result.observed_at == before_midnight


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {},
        {f"item_{index}": "CNY" for index in range(257)},
        {"": "CNY"},
        {" net_profit": "CNY"},
        {1: "CNY"},
        {"net_profit": ""},
        {"net_profit": " CNY"},
        {"net_profit": "C" * 129},
        {"net_profit": "CNY\n"},
    ],
)
def test_unit_mapping_rejects_unbounded_or_ambiguous_declarations(value: object) -> None:
    """Reject malformed caller declarations instead of silently normalizing units."""
    with pytest.raises(ValueError):
        fetcher_module._validate_unit_mapping(value)


def test_unit_mapping_reader_failure_is_reported_without_leaking_exception() -> None:
    """A hostile mapping implementation cannot escape the bounded profile parser."""

    class BrokenItems(dict[str, str]):
        def items(self) -> Any:
            raise RuntimeError("synthetic mapping failure")

    with pytest.raises(ValueError, match="could not be read safely") as error:
        fetcher_module._validate_unit_mapping(BrokenItems({"net_profit": "CNY"}))
    assert "synthetic mapping failure" not in str(error.value)


@pytest.mark.parametrize("version", [None, "", " padded", "padded ", "v" * 129, "v\n1"])
def test_unit_profile_version_must_be_a_bounded_trimmed_token(version: object) -> None:
    digest_function: Any = canonical_native_unit_mapping_sha256
    with pytest.raises(ValueError, match="bounded trimmed token"):
        digest_function(
            statement_type="income",
            version=version,
            native_units={"net_profit": "CNY"},
        )


def test_unit_digest_rejects_unknown_statement_even_with_valid_units() -> None:
    digest_function: Any = canonical_native_unit_mapping_sha256
    invalid_statement = "INCOME"
    with pytest.raises(ValueError, match="supported exact token"):
        digest_function(
            statement_type=invalid_statement,
            version="synthetic-v1",
            native_units={"net_profit": "CNY"},
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("statement_type", "INCOME", "supported exact token"),
        ("version", " padded", "bounded trimmed token"),
        ("mapping_sha256_declared", "A" * 64, "lowercase SHA-256"),
        ("evidence_ref", "", "bounded nonempty operator reference"),
        ("evidence_ref", " synthetic ", "bounded nonempty operator reference"),
        ("evidence_ref", "x\nref", "bounded nonempty operator reference"),
    ],
)
def test_unit_profile_constructor_rejects_invalid_declaration_fields(
    field: str,
    value: object,
    message: str,
) -> None:
    profile = _unit_profiles()["income"]
    arguments: dict[str, object] = {
        "statement_type": "income",
        "version": profile.version,
        "native_units": profile.native_units,
        "mapping_sha256_declared": profile.mapping_sha256_declared,
        "evidence_ref": profile.evidence_ref,
    }
    arguments[field] = value
    profile_type: Any = ThsFinancialNativeUnitProfile
    with pytest.raises(ValueError, match=message):
        profile_type(**arguments)


def test_unit_profile_copies_and_freezes_the_operator_mapping() -> None:
    units = {"net_profit": "CNY"}
    digest = canonical_native_unit_mapping_sha256(
        statement_type="income",
        version="synthetic-v1",
        native_units=units,
    )
    profile = ThsFinancialNativeUnitProfile(
        statement_type="income",
        version="synthetic-v1",
        native_units=units,
        mapping_sha256_declared=digest,
        evidence_ref="synthetic:test-only",
    )
    units["net_profit"] = "USD"
    assert profile.native_units["net_profit"] == "CNY"
    with pytest.raises(TypeError):
        setitem(profile.native_units, "net_profit", "USD")


@pytest.mark.parametrize(
    ("dependency", "error_type", "message"),
    [
        ("source_attempt_gate", TypeError, "exact SourceAttemptGate"),
        ("instrument_registry", TypeError, "exact InstrumentReferenceRegistry"),
        ("credentials", TypeError, "exact trusted FuyaoCredentials"),
        ("mock_transport", TypeError, "exact HTTPX MockTransport"),
        ("unit_profiles_type", TypeError, "explicit mapping"),
        ("unit_profiles_keys", ValueError, "income, balance, and cashflow"),
        ("unit_profile_identity", ValueError, "exact statement identities"),
    ],
)
def test_fetcher_constructor_rejects_untrusted_or_incomplete_dependencies(
    gate_engine: Any,
    dependency: str,
    error_type: type[Exception],
    message: str,
) -> None:
    """Construction never accepts duck-typed gates, registries, credentials or profiles."""
    gate = SourceAttemptGate(gate_engine)
    base, _requests, credentials = _fetcher(_financial_body(), gate=gate)
    arguments: dict[str, object] = {
        "credentials": credentials,
        "source_attempt_gate": gate,
        "instrument_registry": base._instrument_registry,
        "unit_profiles": dict(base._unit_profiles),
        "mock_transport": httpx.MockTransport(lambda request: httpx.Response(200)),
    }
    if dependency in {
        "source_attempt_gate",
        "instrument_registry",
        "credentials",
        "mock_transport",
    }:
        arguments[dependency] = object()
    elif dependency == "unit_profiles_type":
        arguments["unit_profiles"] = []
    elif dependency == "unit_profiles_keys":
        arguments["unit_profiles"] = {"income": base._unit_profiles["income"]}
    else:
        arguments["unit_profiles"] = {
            **base._unit_profiles,
            "income": base._unit_profiles["balance"],
        }
    fetcher_type: Any = ThsFinancialObservationFetcher
    try:
        with pytest.raises(error_type, match=message):
            fetcher_type(**arguments)
    finally:
        base.close()


@pytest.mark.parametrize(
    ("field", "value", "expected_code"),
    [
        ("_source_attempt_gate", object(), "source_attempt_gate_binding_invalid"),
        ("credentials.base_url", "https://fuyao.aicubes.cn/redirect", "fuyao_origin_not_allowed"),
        ("_max_attempts", 4, "transport_attempt_limit_invalid"),
        ("_max_attempts", 0, "transport_attempt_limit_invalid"),
    ],
)
def test_private_client_integrity_drift_fails_before_transport_send(
    gate_engine: Any,
    field: str,
    value: object,
    expected_code: str,
) -> None:
    """Mutation of the service-owned gate, origin or retry cap cannot authorize a send."""
    _seed_gate(gate_engine)
    gate = SourceAttemptGate(gate_engine)
    fetcher, requests, _credentials = _fetcher(_financial_body(), gate=gate)
    client = fetcher._client
    target = client.credentials if field == "credentials.base_url" else client
    attribute = "base_url" if field == "credentials.base_url" else field
    original = getattr(target, attribute)
    setattr(target, attribute, value)
    try:
        with (
            request_execution_scope(
                source=SOURCE,
                canonical_model=MODEL,
                operation=RequestOperation.QUERY,
                budget=_request_budget(),
                source_attempt_binding=_binding(),
            ),
            pytest.raises(FinancialObservationFetchError, match=expected_code),
        ):
            fetcher.fetch(
                instrument_id=INSTRUMENT_ID,
                statement_type="income",
                periodicity="annual",
                limit=1,
            )
        assert requests == []
        assert _attempt_rows(gate_engine) == []
    finally:
        setattr(target, attribute, original)
        fetcher.close()


@pytest.mark.parametrize(
    ("scope_values", "expected_code"),
    [
        ([], "trusted_request_scope_required"),
        ((), "trusted_request_scope_required"),
        ((object(),), "trusted_request_scope_invalid"),
        ((_scope(source="other"),), "trusted_request_scope_invalid"),
        ((_scope(canonical_model=None),), "trusted_request_scope_invalid"),
        ((_scope(operation=RequestOperation.STORE),), "trusted_request_scope_invalid"),
        ((replace(_scope(), source_attempt_binding=None),), "trusted_request_scope_invalid"),
    ],
)
def test_scope_shape_and_identity_are_checked_before_authorization(
    monkeypatch: pytest.MonkeyPatch,
    scope_values: object,
    expected_code: str,
) -> None:
    monkeypatch.setattr(fetcher_module, "current_request_scopes", lambda: scope_values)
    with pytest.raises(FinancialObservationFetchError, match=expected_code):
        ThsFinancialObservationFetcher._validate_scope()


@pytest.mark.parametrize(
    "mutation",
    ["wrong_source", "wrong_model", "wrong_operation", "missing_binding", "different_binding"],
)
def test_nested_scope_must_preserve_exact_source_model_operation_and_binding(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    first = _scope()
    values: dict[str, object] = {}
    if mutation == "wrong_source":
        values["source"] = "other"
    elif mutation == "wrong_model":
        values["canonical_model"] = "other"
    elif mutation == "wrong_operation":
        values["operation"] = RequestOperation.STORE
    elif mutation == "missing_binding":
        values["source_attempt_binding"] = None
    else:
        values["source_attempt_binding"] = SourceAttemptBinding(
            grant_id=GRANT_ID,
            expected_revision=2,
            principal_scope=PRINCIPAL,
            run_key="fixture-other-run",
        )
    second = replace(first, **values)
    monkeypatch.setattr(fetcher_module, "current_request_scopes", lambda: (first, second))
    with pytest.raises(FinancialObservationFetchError, match="trusted_request_scope_invalid"):
        ThsFinancialObservationFetcher._validate_scope()


@pytest.mark.parametrize(
    ("decision", "allowed_hosts", "expires_at", "conditions"),
    [
        (GrantDecision.DENIED, frozenset({HOST}), None, ()),
        (GrantDecision.ALLOWED, frozenset({"other.example"}), None, ()),
        (GrantDecision.ALLOWED, frozenset({HOST}), datetime.now(UTC) - timedelta(seconds=1), ()),
        (GrantDecision.ALLOWED, frozenset({HOST}), None, ("rights evidence pending",)),
    ],
)
def test_missing_grant_rights_host_or_live_window_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    decision: GrantDecision,
    allowed_hosts: frozenset[str],
    expires_at: datetime | None,
    conditions: tuple[str, ...],
) -> None:
    budget = _request_budget(
        decision=decision,
        allowed_hosts=allowed_hosts,
        expires_at=expires_at,
        conditions=conditions,
    )
    monkeypatch.setattr(fetcher_module, "current_request_scopes", lambda: (_scope(budget=budget),))
    with pytest.raises(FinancialObservationFetchError, match="trusted_request_scope_unauthorized"):
        ThsFinancialObservationFetcher._validate_scope()


def test_nested_scopes_cannot_replace_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _scope()
    second = replace(first, budget=_request_budget())
    monkeypatch.setattr(fetcher_module, "current_request_scopes", lambda: (first, second))
    with pytest.raises(
        RequestBudgetScopeError,
        match="nested request budget cannot replace active budget",
    ):
        ThsFinancialObservationFetcher._validate_scope()


@pytest.mark.parametrize(
    "instrument_id",
    [None, 600519, "600519.sh", "600519.SH ", "12345.SH", "600519.SH/path"],
)
def test_instrument_identity_requires_an_exact_exchange_qualified_token(
    dependencies: Any,
    instrument_id: object,
) -> None:
    fetcher = dependencies[3]
    with pytest.raises(FinancialObservationFetchError, match="instrument_identity_invalid"):
        fetcher._validate_instrument(instrument_id)


def test_unknown_registry_lifecycle_is_not_treated_as_a_verified_instrument(
    gate_engine: Any,
) -> None:
    base_registry = _instrument_registry()
    lifecycle = replace(base_registry.lifecycles[0], status=LifecycleStatus.UNKNOWN)
    unknown_registry = InstrumentReferenceRegistry(
        registry_version="fixture-registry-unknown-v1",
        evidence_ref="synthetic:test-only",
        lifecycles=(lifecycle,),
    )
    fetcher, _requests, _credentials = _fetcher(
        _financial_body(),
        gate=SourceAttemptGate(gate_engine),
        instrument_registry=unknown_registry,
    )
    try:
        with pytest.raises(FinancialObservationFetchError, match="instrument_identity_unverified"):
            fetcher._validate_instrument(INSTRUMENT_ID)
    finally:
        fetcher.close()


@pytest.mark.parametrize("field", ["input_identifier", "instrument_id", "lifecycle_instrument"])
def test_registry_resolution_identity_drift_is_rejected(
    gate_engine: Any,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    registry = _instrument_registry()
    resolved = registry.resolve("financial_statement", SOURCE, INSTRUMENT_ID)
    if field == "input_identifier":
        altered = replace(resolved, input_identifier="000001.SZ")
    elif field == "instrument_id":
        altered = replace(resolved, instrument_id="000001.SZ")
    else:
        altered_lifecycle = replace(resolved.lifecycle, instrument_id="000001.SZ")
        altered = replace(resolved, lifecycle=altered_lifecycle)
    monkeypatch.setattr(InstrumentReferenceRegistry, "resolve", lambda self, *args: altered)
    fetcher, _requests, _credentials = _fetcher(
        _financial_body(),
        gate=SourceAttemptGate(gate_engine),
        instrument_registry=registry,
    )
    try:
        with pytest.raises(FinancialObservationFetchError, match="instrument_identity_unverified"):
            fetcher._validate_instrument(INSTRUMENT_ID)
    finally:
        fetcher.close()


@pytest.mark.parametrize(
    ("query", "expected_code"),
    [
        ({"statement_type": "INCOME"}, "statement_type_invalid"),
        ({"statement_type": True}, "statement_type_invalid"),
        ({"periodicity": "monthly"}, "periodicity_invalid"),
        ({"periodicity": True}, "periodicity_invalid"),
        (
            {"start_date": datetime(2024, 1, 1), "end_date": date(2024, 12, 31)},
            "date_window_invalid",
        ),
        ({"start_date": date(2025, 1, 1), "end_date": date(2024, 12, 31)}, "date_window_invalid"),
        ({"limit": True}, "period_limit_invalid"),
        ({"limit": 0}, "period_limit_invalid"),
        ({"limit": "1"}, "period_limit_invalid"),
    ],
)
def test_query_validator_rejects_coercion_and_invalid_calendar_windows(
    query: dict[str, object],
    expected_code: str,
) -> None:
    arguments: dict[str, object] = {
        "statement_type": "income",
        "periodicity": "annual",
        "start_date": None,
        "end_date": None,
        "limit": None,
    }
    arguments.update(query)
    validator: Any = ThsFinancialObservationFetcher._validate_query
    with pytest.raises(FinancialObservationFetchError, match=expected_code):
        validator(**arguments)


def test_mapping_digest_drift_in_a_frozen_profile_is_rechecked_before_send(
    dependencies: Any,
) -> None:
    engine, _gate, requests, fetcher, _body, _observed_times = dependencies
    profile = fetcher._unit_profiles["income"]
    original_units = profile.native_units
    object.__setattr__(profile, "native_units", {})
    try:
        with pytest.raises(FinancialObservationFetchError, match="unit_mapping_profile_invalid"):
            fetcher._validated_profile("income")
        assert requests == []
        assert _attempt_rows(engine) == []
    finally:
        object.__setattr__(profile, "native_units", original_units)


def test_exhausted_in_memory_source_budget_stops_before_durable_reservation_or_send(
    gate_engine: Any,
) -> None:
    """A valid grant still cannot exceed its independent per-run attempt ceiling."""
    _seed_gate(gate_engine)
    gate = SourceAttemptGate(gate_engine)
    fetcher, requests, _credentials = _fetcher(_financial_body(), gate=gate)
    budget = _request_budget()
    try:
        with request_execution_scope(
            source=SOURCE,
            canonical_model=MODEL,
            operation=RequestOperation.QUERY,
            budget=budget,
            source_attempt_binding=_binding(),
        ) as active_scope:
            assert isinstance(active_scope, RequestExecutionScope)
            for _ in range(budget.source_attempt_limit):
                budget.reserve_attempt((active_scope,), SOURCE, HOST)
            with pytest.raises(RequestAttemptLimitError, match="allowance exhausted"):
                fetcher.fetch(
                    instrument_id=INSTRUMENT_ID,
                    statement_type="income",
                    periodicity="annual",
                    limit=1,
                )
        assert requests == []
        assert _attempt_rows(gate_engine) == []
    finally:
        fetcher.close()


def test_cancelled_request_scope_never_reaches_authorization_or_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancellation = Event()
    cancellation.set()
    monkeypatch.setattr(
        fetcher_module,
        "current_request_scopes",
        lambda: (replace(_scope(), cancellation=cancellation),),
    )
    with pytest.raises(RequestExecutionCancelledError, match="cancelled"):
        ThsFinancialObservationFetcher._validate_scope()


def test_upstream_request_builder_exception_is_sanitized_before_any_send(
    dependencies: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid internally constructed endpoint parameters do not reserve a send."""
    engine, _gate, requests, fetcher, _body, _observed_times = dependencies

    def fail_build(**_kwargs: object) -> dict[str, object]:
        raise ValueError("synthetic builder detail must not escape")

    monkeypatch.setattr(fetcher_module, "build_financial_statements_request", fail_build)
    with (
        request_execution_scope(
            source=SOURCE,
            canonical_model=MODEL,
            operation=RequestOperation.QUERY,
            budget=_request_budget(),
            source_attempt_binding=_binding(),
        ),
        pytest.raises(
            FinancialObservationFetchError,
            match="upstream_request_parameters_invalid",
        ) as error,
    ):
        fetcher.fetch(
            instrument_id=INSTRUMENT_ID,
            statement_type="income",
            periodicity="annual",
            limit=1,
        )
    assert "synthetic builder detail" not in str(error.value)
    assert requests == []
    assert _attempt_rows(engine) == []


def test_upstream_request_parameters_must_match_validated_query_before_send(
    dependencies: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject mutated builder parameters before a reservation or physical send."""
    engine, _gate, requests, fetcher, _body, _observed_times = dependencies
    monkeypatch.setattr(
        fetcher_module,
        "build_financial_statements_request",
        lambda **_kwargs: {
            "thscode": INSTRUMENT_ID,
            "period": "annual",
            "limit": 2,
            "unexpected": "not in the validated query",
        },
    )
    with (
        request_execution_scope(
            source=SOURCE,
            canonical_model=MODEL,
            operation=RequestOperation.QUERY,
            budget=_request_budget(),
            source_attempt_binding=_binding(),
        ),
        pytest.raises(
            FinancialObservationFetchError,
            match="upstream_request_parameters_invalid",
        ),
    ):
        fetcher.fetch(
            instrument_id=INSTRUMENT_ID,
            statement_type="income",
            periodicity="annual",
            limit=1,
        )
    assert requests == []
    assert _attempt_rows(engine) == []


def _successful_result(dependencies: Any) -> FinancialObservationFetchResult:
    """Return one fixture result through the real MockTransport and strict decoder."""
    fetcher = dependencies[3]
    with request_execution_scope(
        source=SOURCE,
        canonical_model=MODEL,
        operation=RequestOperation.QUERY,
        budget=_request_budget(),
        source_attempt_binding=_binding(),
    ):
        return fetcher.fetch(
            instrument_id=INSTRUMENT_ID,
            statement_type="income",
            periodicity="annual",
            limit=1,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("raw_body", b"", "raw_body"),
        ("raw_body", bytearray(b"{}"), "raw_body"),
        ("request_summary_sha256", "invalid", "lowercase SHA-256 or None"),
        ("body_sha256", "0" * 64, "does not match raw_body"),
        ("source_request_id", " padded ", "nonempty trimmed token"),
        ("source_request_id", "", "nonempty trimmed token"),
        ("observed_at", datetime.now(UTC).replace(tzinfo=None), "timezone-aware"),
        ("observed_at", datetime.now(SHANGHAI), "zero-offset UTC timezone"),
        ("status_code", True, "successful Fuyao response status"),
        ("status_code", 500, "successful Fuyao response status"),
        ("observations", [], "immutable tuples"),
        ("quarantined_rows", [], "immutable tuples"),
        ("coverage_assessment", "complete", "cannot assert completeness"),
        ("usability_status", "PARTIAL", "does not match decoded rows"),
    ],
)
def test_fetch_result_rejects_unbound_or_noncanonical_metadata(
    dependencies: Any,
    field: str,
    value: object,
    message: str,
) -> None:
    result = _successful_result(dependencies)
    with pytest.raises(ValueError, match=message):
        replace(result, **{field: value})


def test_fetch_result_rejects_observation_reference_from_another_body(
    dependencies: Any,
) -> None:
    result = _successful_result(dependencies)
    altered = replace(result.observations[0], body_sha256="0" * 64)
    with pytest.raises(ValueError, match="not bound to raw_body"):
        replace(result, observations=(altered, *result.observations[1:]))


def test_fetch_result_rejects_inconsistent_local_transport_trace(dependencies: Any) -> None:
    """Reject local trace fields that disagree with this decoded success result."""
    result = _successful_result(dependencies)
    final_trace = result.attempt_trace[-1]

    with pytest.raises(ValueError, match="final local attempt trace"):
        replace(result, attempt_trace=(replace(final_trace, http_status=201),))

    with pytest.raises(ValueError, match="source request identifier"):
        replace(result, attempt_trace=(replace(final_trace, source_request_id="other-id"),))

    with pytest.raises(ValueError, match="unsupported local trace type"):
        replace(result, attempt_trace=(object(),))


def test_fetch_result_rejects_quarantine_reference_from_different_body(
    dependencies: Any,
) -> None:
    result = _successful_result(dependencies)
    quarantine = QuarantinedFinancialObservationRow(
        body_sha256="0" * 64,
        row_pointer="/data/item/0",
        disclosure_pointer=None,
        reason="disclosure_date_missing",
    )
    with pytest.raises(ValueError, match="quarantine reference is not bound"):
        replace(result, quarantined_rows=(quarantine,))


@pytest.mark.parametrize("field", ["source_request_id", "unit_mapping_sha256", "body_sha256"])
def test_decoded_response_binding_mismatch_is_rejected_after_mock_send(
    dependencies: Any,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    engine, _gate, requests, fetcher, _body, _observed_times = dependencies
    original_decode: Any = fetcher_module.decode_financial_observation_body

    def altered_decode(*args: object, **kwargs: object) -> FinancialObservationDecodeResult:
        decoded = original_decode(*args, **kwargs)
        if field == "source_request_id":
            return replace(decoded, source_request_id="different-request-id")
        if field == "unit_mapping_sha256":
            return replace(decoded, native_unit_mapping_sha256_declared="0" * 64)
        return replace(decoded, body_sha256="0" * 64)

    monkeypatch.setattr(fetcher_module, "decode_financial_observation_body", altered_decode)
    with (
        request_execution_scope(
            source=SOURCE,
            canonical_model=MODEL,
            operation=RequestOperation.QUERY,
            budget=_request_budget(),
            source_attempt_binding=_binding(),
        ),
        pytest.raises(FinancialObservationFetchError, match="response_binding_mismatch"),
    ):
        fetcher.fetch(
            instrument_id=INSTRUMENT_ID,
            statement_type="income",
            periodicity="annual",
            limit=1,
        )
    assert len(requests) == 1
    assert len(_attempt_rows(engine)) == 1


@pytest.mark.parametrize("identity_field", ["instrument_id", "statement_type", "periodicity"])
def test_decoded_observation_identity_must_match_the_exact_request(
    dependencies: Any,
    monkeypatch: pytest.MonkeyPatch,
    identity_field: str,
) -> None:
    original_decode: Any = fetcher_module.decode_financial_observation_body

    def alter_observation(*args: object, **kwargs: object) -> FinancialObservationDecodeResult:
        decoded = original_decode(*args, **kwargs)
        reference = decoded.observations[0]
        if identity_field == "instrument_id":
            changed_observation = replace(reference.observation, instrument_id="000001.SZ")
        elif identity_field == "statement_type":
            changed_observation = replace(reference.observation, statement_type="balance")
        else:
            changed_observation = replace(reference.observation, periodicity="quarterly")
        changed_reference = replace(reference, observation=changed_observation)
        return replace(decoded, observations=(changed_reference, *decoded.observations[1:]))

    monkeypatch.setattr(fetcher_module, "decode_financial_observation_body", alter_observation)
    fetcher = dependencies[3]
    with (
        request_execution_scope(
            source=SOURCE,
            canonical_model=MODEL,
            operation=RequestOperation.QUERY,
            budget=_request_budget(),
            source_attempt_binding=_binding(),
        ),
        pytest.raises(
            FinancialObservationFetchError,
            match="decoded_observation_binding_mismatch",
        ),
    ):
        fetcher.fetch(
            instrument_id=INSTRUMENT_ID,
            statement_type="income",
            periodicity="annual",
            limit=1,
        )


def test_decoded_observation_reference_must_share_the_exact_body_digest(
    dependencies: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_decode: Any = fetcher_module.decode_financial_observation_body

    def alter_reference(*args: object, **kwargs: object) -> FinancialObservationDecodeResult:
        decoded = original_decode(*args, **kwargs)
        changed = replace(decoded.observations[0], body_sha256="0" * 64)
        return replace(decoded, observations=(changed, *decoded.observations[1:]))

    monkeypatch.setattr(fetcher_module, "decode_financial_observation_body", alter_reference)
    fetcher = dependencies[3]
    with (
        request_execution_scope(
            source=SOURCE,
            canonical_model=MODEL,
            operation=RequestOperation.QUERY,
            budget=_request_budget(),
            source_attempt_binding=_binding(),
        ),
        pytest.raises(
            FinancialObservationFetchError,
            match="decoded_observation_binding_mismatch",
        ),
    ):
        fetcher.fetch(
            instrument_id=INSTRUMENT_ID,
            statement_type="income",
            periodicity="annual",
            limit=1,
        )


def test_decoded_quarantine_reference_must_share_the_exact_body_digest(
    gate_engine: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_gate(gate_engine)
    fetcher, _requests, _credentials = _fetcher(
        _financial_body(include_disclosure=False),
        gate=SourceAttemptGate(gate_engine),
    )
    original_decode: Any = fetcher_module.decode_financial_observation_body

    def alter_quarantine(*args: object, **kwargs: object) -> FinancialObservationDecodeResult:
        decoded = original_decode(*args, **kwargs)
        changed = replace(decoded.quarantined_rows[0], body_sha256="0" * 64)
        return replace(decoded, quarantined_rows=(changed, *decoded.quarantined_rows[1:]))

    monkeypatch.setattr(fetcher_module, "decode_financial_observation_body", alter_quarantine)
    try:
        with (
            request_execution_scope(
                source=SOURCE,
                canonical_model=MODEL,
                operation=RequestOperation.QUERY,
                budget=_request_budget(),
                source_attempt_binding=_binding(),
            ),
            pytest.raises(FinancialObservationFetchError, match="quarantine_body_binding_mismatch"),
        ):
            fetcher.fetch(
                instrument_id=INSTRUMENT_ID,
                statement_type="income",
                periodicity="annual",
                limit=1,
            )
    finally:
        fetcher.close()


@pytest.mark.parametrize(
    "invalid_time",
    [
        datetime.now(UTC).replace(tzinfo=None),
        datetime.min.replace(tzinfo=timezone(timedelta(hours=1))),
    ],
)
def test_invalid_system_clock_output_fails_after_a_single_mock_response(
    dependencies: Any,
    monkeypatch: pytest.MonkeyPatch,
    invalid_time: datetime,
) -> None:
    engine, _gate, requests, fetcher, _body, observed_times = dependencies
    monkeypatch.setattr(fetcher_module, "_utc_now", lambda: invalid_time)
    with (
        request_execution_scope(
            source=SOURCE,
            canonical_model=MODEL,
            operation=RequestOperation.QUERY,
            budget=_request_budget(),
            source_attempt_binding=_binding(),
        ),
        pytest.raises(FinancialObservationFetchError, match="response_observation_time_invalid"),
    ):
        fetcher.fetch(
            instrument_id=INSTRUMENT_ID,
            statement_type="income",
            periodicity="annual",
            limit=1,
        )
    assert len(requests) == 1
    assert observed_times == []
    assert len(_attempt_rows(engine)) == 1


def test_response_with_empty_entity_body_is_rejected_after_mock_transport_receive(
    dependencies: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, _gate, requests, fetcher, _body, _observed_times = dependencies
    original_get: Any = fetcher._client.get

    def erase_buffered_entity(*args: object, **kwargs: object) -> Any:
        response = original_get(*args, **kwargs)
        return replace(response, raw_content=b"")

    monkeypatch.setattr(fetcher._client, "get", erase_buffered_entity)
    with (
        request_execution_scope(
            source=SOURCE,
            canonical_model=MODEL,
            operation=RequestOperation.QUERY,
            budget=_request_budget(),
            source_attempt_binding=_binding(),
        ),
        pytest.raises(FinancialObservationFetchError, match="response_body_empty_or_invalid"),
    ):
        fetcher.fetch(
            instrument_id=INSTRUMENT_ID,
            statement_type="income",
            periodicity="annual",
            limit=1,
        )
    assert len(requests) == 1
    assert len(_attempt_rows(engine)) == 1


def test_fetch_result_rejects_timezone_without_a_usable_offset(dependencies: Any) -> None:
    class MissingOffset(tzinfo):
        def utcoffset(self, _dt: datetime | None) -> timedelta | None:
            return None

    result = _successful_result(dependencies)
    invalid_time = datetime(2025, 1, 1, tzinfo=MissingOffset())
    with pytest.raises(ValueError, match="timezone-aware"):
        replace(result, observed_at=invalid_time)


def test_fetcher_context_manager_closes_the_owned_http_client(gate_engine: Any) -> None:
    fetcher, _requests, _credentials = _fetcher(
        _financial_body(),
        gate=SourceAttemptGate(gate_engine),
    )
    assert fetcher.__enter__() is fetcher
    fetcher.__exit__(None, None, None)
    assert fetcher._client._client.is_closed


def _metadata_only_body(rows: list[object]) -> bytes:
    """Encode fiscal metadata independently for second-pass integrity tests."""
    return json.dumps({"data": {"item": rows}}, separators=(",", ":")).encode("utf-8")


def _quarantine_pointers(*pointers: str) -> tuple[QuarantinedFinancialObservationRow, ...]:
    """Create synthetic row references for validator pointer-shape tests."""
    return tuple(
        QuarantinedFinancialObservationRow(
            body_sha256="a" * 64,
            row_pointer=pointer,
            disclosure_pointer=None,
            reason="disclosure_date_missing",
        )
        for pointer in pointers
    )


def test_response_metadata_validator_returns_fiscal_period_from_the_exact_row(
    dependencies: Any,
) -> None:
    result = _successful_result(dependencies)
    periods = fetcher_module._validated_response_row_periods(
        result.raw_body,
        observations=result.observations,
        quarantined_rows=result.quarantined_rows,
    )
    assert periods == (REPORT_PERIOD,)


@pytest.mark.parametrize(
    "body",
    [
        None,
        b"",
        b"x" * (fetcher_module.MAX_BODY_BYTES + 1),
        b"\xff",
        b"{",
        b"[]",
        b'{"data":{"item":null}}',
        _metadata_only_body([_financial_row()] * (fetcher_module.MAX_FINANCIAL_ROWS + 1)),
    ],
)
def test_response_metadata_validator_rejects_invalid_or_unbounded_bodies(body: object) -> None:
    metadata_validator: Any = fetcher_module._validated_response_row_periods
    with pytest.raises(FinancialObservationFetchError, match="response_row_metadata_invalid"):
        metadata_validator(
            body,
            observations=(),
            quarantined_rows=(),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("fiscal_year", True),
        ("fiscal_period", 4),
        ("fiscal_period", "Q5"),
        ("fiscal_year", 10000),
        ("period_end_ms", "not-a-timestamp"),
        ("period_end_ms", shanghai_midnight_millis(date(2023, 12, 31))),
    ],
)
def test_response_metadata_validator_rejects_invalid_fiscal_dates(
    field: str,
    value: object,
) -> None:
    row = _financial_row()
    row[field] = value
    with pytest.raises(FinancialObservationFetchError, match="response_row_period_invalid"):
        fetcher_module._validated_response_row_periods(
            _metadata_only_body([row]),
            observations=(),
            quarantined_rows=_quarantine_pointers("/data/item/0"),
        )


def test_response_metadata_validator_rejects_non_object_rows() -> None:
    with pytest.raises(FinancialObservationFetchError, match="response_row_metadata_invalid"):
        fetcher_module._validated_response_row_periods(
            _metadata_only_body(["not-an-object"]),
            observations=(),
            quarantined_rows=_quarantine_pointers("/data/item/0"),
        )


@pytest.mark.parametrize(
    "observations,quarantined_rows",
    [
        ((), ()),
        ((), _quarantine_pointers("/data/item/99")),
        ((), _quarantine_pointers("/data/item/0", "/data/item/0")),
    ],
)
def test_response_metadata_validator_rejects_missing_duplicate_or_extra_row_pointers(
    observations: tuple[Any, ...],
    quarantined_rows: tuple[QuarantinedFinancialObservationRow, ...],
) -> None:
    with pytest.raises(FinancialObservationFetchError, match="response_row_pointer_mismatch"):
        fetcher_module._validated_response_row_periods(
            _metadata_only_body([_financial_row()]),
            observations=observations,
            quarantined_rows=quarantined_rows,
        )


def test_response_metadata_validator_rejects_observation_and_quarantine_overlap(
    dependencies: Any,
) -> None:
    result = _successful_result(dependencies)
    with pytest.raises(FinancialObservationFetchError, match="response_row_pointer_mismatch"):
        fetcher_module._validated_response_row_periods(
            result.raw_body,
            observations=result.observations,
            quarantined_rows=_quarantine_pointers("/data/item/0"),
        )


def test_response_metadata_validator_rejects_row_period_different_from_observation(
    dependencies: Any,
) -> None:
    result = _successful_result(dependencies)
    reference = result.observations[0]
    changed_observation = replace(reference.observation, report_period=date(2023, 12, 31))
    changed_reference = replace(reference, observation=changed_observation)
    with pytest.raises(
        FinancialObservationFetchError,
        match="response_row_period_binding_mismatch",
    ):
        fetcher_module._validated_response_row_periods(
            result.raw_body,
            observations=(changed_reference, *result.observations[1:]),
            quarantined_rows=result.quarantined_rows,
        )
