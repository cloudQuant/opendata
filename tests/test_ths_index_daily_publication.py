"""Offline four-index publication bridge from the THS provider to the v2 API."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import create_engine

from opendata.api.dependencies import Principal, get_current_principal
from opendata.api.published_access import DatasetGrant, DatasetGrantRegistry
from opendata.api.published_data import (
    get_dataset_contracts,
    get_dataset_grants,
    get_publication_store,
    router,
)
from opendata.data.dataset_contract_registry import DatasetContractRegistry
from opendata.data.models.market import Bar
from opendata.data.protocol import FetchContext
from opendata.data.providers.ths import endpoints as ths_endpoints
from opendata.data.providers.ths.index_daily_publication import (
    INDEX_DATASET_ID_BY_SYMBOL,
    INDEX_JOB_ID_BY_SYMBOL,
    INDEX_SYMBOL_BY_DATASET_ID,
    SSE50_INDEX_DATASET_ID,
    SSE50_INDEX_SCHEMA_VERSION,
    SSE50_INDEX_SEMANTIC_VERSION,
    SSE50_INDEX_SOURCE_ID,
    SSE50_INDEX_SYMBOL,
    build_index_daily_contract,
    build_sse50_index_daily_contract,
)
from opendata.data.providers.ths.models import index_daily as ths_index_model
from opendata.data.providers.ths.models.index_daily import ThsIndexDailyFetcher
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestBudgetProfile,
    RequestGrant,
    RequestOperation,
)
from opendata.models.user import User, UserRole
from opendata.pipeline import ths_index_daily_publication as job_module
from opendata.pipeline.publication_store import PUBLICATION_METADATA, PublicationStore
from opendata.pipeline.run_identity import RunRequest
from opendata.pipeline.run_service import (
    NormalizedBatch,
    NormalizedRecord,
    ReviewedJob,
    RunExecutionContext,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

UTC = timezone.utc
START = date(2026, 10, 21)
END = date(2026, 10, 22)


def _bars(symbol: str = SSE50_INDEX_SYMBOL) -> tuple[Bar, Bar]:
    """Return a synthetic two-day OHLCV series for fixture transport only."""
    return (
        Bar(
            symbol=symbol,
            trade_date=START,
            open=4_600.0,
            high=4_650.0,
            low=4_590.0,
            close=4_640.0,
            volume=950_000_000.0,
            amount=4_250_000_000.0,
        ),
        Bar(
            symbol=symbol,
            trade_date=END,
            open=4_640.0,
            high=4_680.0,
            low=4_620.0,
            close=4_670.0,
            volume=980_000_000.0,
            amount=4_410_000_000.0,
        ),
    )


def _request(symbol: str = SSE50_INDEX_SYMBOL) -> RunRequest:
    """Build one exact symbol request used by the offline fixtures."""
    return job_module.build_index_daily_backfill_request(
        symbol=symbol,
        start_date=START,
        end_date=END,
        idempotency_key=f"index-{symbol.replace('.', '-')}-offline-fixture-001",
    )


def _context() -> RunExecutionContext:
    """Build a local no-op execution context without a durable worker lease."""
    return RunExecutionContext(
        run_key="sse50-offline-fixture-run",
        worker="sse50-offline-fixture",
        fencing_token=1,
        _checkpoint=lambda: None,
    )


def _job(symbol: str = SSE50_INDEX_SYMBOL) -> ReviewedJob:
    """Build the real disabled job around one exact synthetic index contract."""
    contract = build_index_daily_contract(symbol)
    return job_module.build_index_daily_job(symbol, DatasetContractRegistry([contract]))


def _fixture_fetch_context(timeout: float | None) -> FetchContext:
    """Grant one bounded fake-provider attempt inside this test only."""
    grant = RequestGrant(
        source="ths",
        canonical_model="index_daily",
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="test-fixture:offline-only:mock-transport",
        task_attempts=1,
        source_attempts=1,
        allowed_hosts=frozenset({"fuyao.test"}),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        profile=RequestBudgetProfile.BOUNDED_REQUEST,
    )
    budget = RequestBudget(
        task_attempts=1,
        source_attempts=1,
        grants=(grant,),
        profile=RequestBudgetProfile.BOUNDED_REQUEST,
    )
    return FetchContext(timeout=timeout, request_budget=budget)


def _api_app(
    store: PublicationStore,
    contracts: DatasetContractRegistry,
    grants: DatasetGrantRegistry,
) -> FastAPI:
    """Mount the existing v2 router with isolated fixture dependencies."""
    app = FastAPI()
    app.include_router(router)
    principal = Principal(
        user=User(
            id=17,
            username="sse50-offline-test",
            email="sse50-offline-test@example.test",
            hashed_password="not-a-real-password-hash",
            role=UserRole.USER,
            is_active=True,
        )
    )
    app.dependency_overrides[get_current_principal] = lambda: principal
    app.dependency_overrides[get_dataset_grants] = lambda: grants
    app.dependency_overrides[get_dataset_contracts] = lambda: contracts
    app.dependency_overrides[get_publication_store] = lambda: store
    return app


async def _get(
    app: FastAPI,
    path: str,
    *,
    params: dict[str, str] | None = None,
) -> Response:
    """Issue one ASGI request without starting the application lifespan."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get(path, params=params)


def test_contracts_and_tasks_are_one_to_one_and_remain_disabled() -> None:
    """Each whitelisted symbol has its own exact dataset and disabled job."""
    contracts = [build_index_daily_contract(symbol) for symbol in INDEX_DATASET_ID_BY_SYMBOL]
    registry = DatasetContractRegistry(contracts)
    seen_dataset_ids: set[str] = set()

    for symbol, contract in zip(INDEX_DATASET_ID_BY_SYMBOL, contracts, strict=True):
        job = job_module.build_index_daily_job(symbol, registry)
        assert contract.dataset_id == INDEX_DATASET_ID_BY_SYMBOL[symbol]
        assert INDEX_SYMBOL_BY_DATASET_ID[contract.dataset_id] == symbol
        assert contract.schema_version == SSE50_INDEX_SCHEMA_VERSION
        assert contract.semantic_version == SSE50_INDEX_SEMANTIC_VERSION
        assert contract.source.source_id == SSE50_INDEX_SOURCE_ID
        assert tuple(field.name for field in contract.fields) == (
            "symbol",
            "trade_date",
            "open",
            "high",
            "low",
            "close",
            "volume",
        )
        assert contract.field_units == {
            "open": "index_points",
            "high": "index_points",
            "low": "index_points",
            "close": "index_points",
            "volume": "shares",
        }
        assert contract.business_key == ("symbol", "trade_date")
        assert job.task.dataset_id == contract.dataset_id
        assert job.task.job_id == INDEX_JOB_ID_BY_SYMBOL[symbol]
        assert job.task.enabled is False
        assert job.task.schedule is None
        assert job.reviewed is False
        other_symbol = next(
            candidate for candidate in INDEX_DATASET_ID_BY_SYMBOL if candidate != symbol
        )
        with pytest.raises(ValueError, match="different index symbol"):
            job.normalize((_bars(other_symbol)[0],), _request(symbol), _context())
        seen_dataset_ids.add(contract.dataset_id)

    assert len(seen_dataset_ids) == len(INDEX_DATASET_ID_BY_SYMBOL) == 4
    assert INDEX_DATASET_ID_BY_SYMBOL["000906.SH"] == "cn.csi800.index.daily"
    assert build_sse50_index_daily_contract().dataset_id == SSE50_INDEX_DATASET_ID
    with pytest.raises(ValueError, match="four reviewed benchmark codes"):
        build_index_daily_contract("883300.TI")


def test_unknown_ths_authorization_stops_before_provider_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UNKNOWN rights block the real provider call before it can send a request."""
    contract = build_sse50_index_daily_contract()
    job = job_module.build_sse50_index_daily_job(DatasetContractRegistry([contract]))
    provider_calls = 0

    def fail_if_called(self: ThsIndexDailyFetcher, **kwargs: object) -> object:
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError("the THS provider must not run while source rights are unknown")

    monkeypatch.setattr(ThsIndexDailyFetcher, "fetch", fail_if_called)

    with pytest.raises(job_module.ThsIndexSourceAuthorizationUnknownError):
        job.fetch(_request(), _context())

    assert provider_calls == 0
    wrong_index_request = RunRequest(
        run_type="backfill",
        query={},
        partition={"symbol": "000300.SH"},
        idempotency_key="other-index-fixture-001",
        backfill_start=START,
        backfill_end=END,
    )
    with pytest.raises(ValueError, match="fixed-symbol"):
        job.fetch(wrong_index_request, _context())


@pytest.mark.asyncio
async def test_fixture_provider_publication_and_v2_read_are_single_index_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Four fixture series stay isolated through provider, writer, and v2 reads."""
    contract_by_symbol = {
        symbol: build_index_daily_contract(symbol) for symbol in INDEX_DATASET_ID_BY_SYMBOL
    }
    contracts = DatasetContractRegistry(list(contract_by_symbol.values()))
    jobs = {
        symbol: job_module.build_index_daily_job(symbol, contracts)
        for symbol in INDEX_DATASET_ID_BY_SYMBOL
    }
    source_calls: list[tuple[str, date, date]] = []

    monkeypatch.setattr(job_module, "_require_ths_source_authorization", lambda: None)
    monkeypatch.setattr(job_module, "FetchContext", _fixture_fetch_context)

    @contextmanager
    def fake_client(*, timeout_seconds: float) -> Iterator[object]:
        assert timeout_seconds == 30
        yield object()

    def resolve_index_code(client: object, symbol: str) -> str:
        assert symbol in INDEX_DATASET_ID_BY_SYMBOL
        return symbol

    def fetch_index_daily_bars(
        client: object,
        *,
        symbol: str,
        start: date,
        end: date,
    ) -> tuple[Bar, ...]:
        assert symbol in INDEX_DATASET_ID_BY_SYMBOL
        assert start == START
        assert end == END + timedelta(days=1)
        source_calls.append((symbol, start, end))
        return _bars(symbol)

    monkeypatch.setattr(ths_index_model, "client", fake_client)
    monkeypatch.setattr(ths_index_model, "resolve_index_code", resolve_index_code)
    monkeypatch.setattr(ths_endpoints, "fetch_index_daily_bars", fetch_index_daily_bars)

    engine = create_engine(f"sqlite:///{tmp_path / 'ths-index-publication.sqlite'}")
    try:
        PUBLICATION_METADATA.create_all(engine)
        store = PublicationStore(
            engine,
            cursor_signing_key=b"test-fixture-cursor-signing-key-32-bytes!",
        )
        fixture_grants: list[DatasetGrant] = []
        for symbol in INDEX_DATASET_ID_BY_SYMBOL:
            contract = contract_by_symbol[symbol]
            job = jobs[symbol]
            request = _request(symbol)
            batch = job.normalize(job.fetch(request, _context()), request, _context())
            assert len(batch.records) == 2
            assert all(record.payload["symbol"] == symbol for record in batch.records)
            assert all("amount" not in record.payload for record in batch.records)
            assert all(
                record.business_date in {START, END}
                and record.observed_at is None
                and record.source_published_at is None
                and record.available_at is None
                for record in batch.records
            )
            assert batch.provenance["dataset_id"] == contract.dataset_id
            assert batch.provenance["source_rights_status"] == "unknown"
            assert batch.provenance["source_comparison_status"] == "unknown"
            assert batch.provenance["source_data_origin_status"] == "unknown"
            assert batch.provenance["coverage_status"] == "unknown"
            assert batch.provenance["source_history_boundary"] == "unknown"

            other_symbol = next(
                candidate for candidate in INDEX_DATASET_ID_BY_SYMBOL if candidate != symbol
            )
            wrong_payload = dict(batch.records[0].payload)
            wrong_payload["symbol"] = other_symbol
            wrong_record: NormalizedRecord = replace(batch.records[0], payload=wrong_payload)
            wrong_batch: NormalizedBatch = replace(batch, records=(wrong_record,))
            with engine.begin() as connection:
                with pytest.raises(ValueError, match="different index symbol"):
                    job.publish(
                        connection,
                        job.task,
                        request,
                        wrong_batch,
                        f"fixture-cross-symbol-{symbol.replace('.', '-')}",
                    )
                with pytest.raises(ValueError, match="empty index batches"):
                    job.publish(
                        connection,
                        job.task,
                        request,
                        replace(batch, records=()),
                        f"fixture-empty-batch-{symbol.replace('.', '-')}",
                    )

            with engine.begin() as connection:
                receipt = job.publish(
                    connection,
                    job.task,
                    request,
                    batch,
                    f"fixture-publication-{symbol.replace('.', '-')}",
                )
            assert (receipt.dataset_id, receipt.record_count) == (contract.dataset_id, 2)
            fixture_grants.append(
                DatasetGrant(
                    dataset_id=contract.dataset_id,
                    domain="index",
                    source_id=SSE50_INDEX_SOURCE_ID,
                    permitted_operations=(
                        "data.read",
                        "coverage.read",
                        "snapshot.create",
                        "snapshot.read",
                    ),
                    purposes=("research",),
                    valid_from=datetime(2020, 1, 1, tzinfo=UTC),
                    valid_until=datetime(2100, 1, 1, tzinfo=UTC),
                    review_evidence="OD-TEST-INDEX-001",
                )
            )

        assert source_calls == [
            (symbol, START, END + timedelta(days=1)) for symbol in INDEX_DATASET_ID_BY_SYMBOL
        ]
        app = _api_app(store, contracts, DatasetGrantRegistry(fixture_grants))
        for symbol, dataset_id in INDEX_DATASET_ID_BY_SYMBOL.items():
            page = await _get(
                app,
                f"/api/v2/datasets/{dataset_id}/records",
                params={
                    "start_date": START.isoformat(),
                    "end_date": (END + timedelta(days=1)).isoformat(),
                },
            )
            assert page.status_code == 200
            rows = page.json()["rows"]
            assert len(rows) == 2
            assert all(row["payload"]["symbol"] == symbol for row in rows)
            assert all("amount" not in row["payload"] for row in rows)

            coverage = await _get(
                app,
                f"/api/v2/datasets/{dataset_id}/coverage",
                params={
                    "start_date": START.isoformat(),
                    "end_date": (END + timedelta(days=1)).isoformat(),
                },
            )
            assert coverage.status_code == 200
            assert coverage.json()["coverage"]["status"] == "unknown"
            assert coverage.json()["coverage"]["scope"] == "unknown"

        first_dataset_id = next(iter(INDEX_DATASET_ID_BY_SYMBOL.values()))
        no_grant_app = _api_app(store, contracts, DatasetGrantRegistry([]))
        denied = await _get(
            no_grant_app,
            f"/api/v2/datasets/{first_dataset_id}/records",
        )
        assert denied.status_code == 403
    finally:
        engine.dispose()


def test_backfill_request_builders_enforce_one_symbol_and_inclusive_date_order() -> None:
    """Both public builders preserve the requested symbol and inclusive bounds."""
    sse50_request = job_module.build_sse50_index_daily_backfill_request(
        start_date=START,
        end_date=END,
        idempotency_key="fixture-sse50-range-001",
    )
    assert sse50_request.partition == {"symbol": SSE50_INDEX_SYMBOL}
    assert (sse50_request.backfill_start, sse50_request.backfill_end) == (START, END)

    csi500_request = job_module.build_index_daily_backfill_request(
        symbol="000905.SH",
        start_date=START,
        end_date=END,
        idempotency_key="fixture-csi500-range-001",
    )
    assert csi500_request.partition == {"symbol": "000905.SH"}

    with pytest.raises(ValueError, match="four reviewed benchmark codes"):
        job_module.build_index_daily_backfill_request(
            symbol="000852.SH",
            start_date=START,
            end_date=END,
            idempotency_key="fixture-unsupported-index-001",
        )
    with pytest.raises(TypeError, match="start_date must be a date"):
        job_module.build_index_daily_backfill_request(
            symbol=SSE50_INDEX_SYMBOL,
            start_date=datetime(2026, 10, 21, tzinfo=UTC),
            end_date=END,
            idempotency_key="fixture-datetime-start-001",
        )
    with pytest.raises(TypeError, match="end_date must be a date"):
        job_module.build_index_daily_backfill_request(
            symbol=SSE50_INDEX_SYMBOL,
            start_date=START,
            end_date=datetime(2026, 10, 22, tzinfo=UTC),
            idempotency_key="fixture-datetime-end-001",
        )
    with pytest.raises(ValueError, match="end_date must not be before start_date"):
        job_module.build_index_daily_backfill_request(
            symbol=SSE50_INDEX_SYMBOL,
            start_date=END,
            end_date=START,
            idempotency_key="fixture-reversed-range-001",
        )


def test_job_builder_rejects_dataset_identity_and_contract_mismatches() -> None:
    """A job cannot bind to a missing, other-index, or altered contract."""
    with pytest.raises(ValueError, match="four reviewed benchmark codes"):
        job_module.build_index_daily_job("000852.SH", DatasetContractRegistry([]))
    with pytest.raises(TypeError, match="contract_registry"):
        job_module.build_index_daily_job(SSE50_INDEX_SYMBOL, object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="exact single-index contract"):
        job_module.build_index_daily_job(SSE50_INDEX_SYMBOL, DatasetContractRegistry([]))

    csi500_contract = build_index_daily_contract("000905.SH")
    with pytest.raises(ValueError, match="exact single-index contract"):
        job_module.build_sse50_index_daily_job(DatasetContractRegistry([csi500_contract]))

    expected = build_index_daily_contract(SSE50_INDEX_SYMBOL)
    altered = expected.model_copy(update={"field_units": {"close": "currency"}})
    with pytest.raises(ValueError, match="does not match this job"):
        job_module.build_index_daily_job(SSE50_INDEX_SYMBOL, DatasetContractRegistry([altered]))


def test_normalize_rejects_empty_results_and_non_bar_provider_rows() -> None:
    """The real job normalizer fails closed on absent or malformed source rows."""
    job = _job()
    request = _request()
    for raw in ((), []):
        with pytest.raises(ValueError, match="non-empty tuple"):
            job.normalize(raw, request, _context())
    with pytest.raises(ValueError, match="invalid row"):
        job.normalize((object(),), request, _context())


def test_normalize_rejects_duplicate_and_out_of_order_business_dates() -> None:
    """The business key permits one row per date and requires ascending dates."""
    job = _job()
    request = _request()
    first, second = _bars()
    duplicate_day = second.model_copy(update={"trade_date": START})
    for rows in ((first, duplicate_day), (second, first)):
        with pytest.raises(ValueError, match="unique and date ordered"):
            job.normalize(rows, request, _context())


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"symbol": "000300.SH"}, "different index symbol"),
        ({"trade_date": END + timedelta(days=1)}, "outside the requested window"),
        ({"open": float("nan")}, "non-finite OHLCV"),
        ({"volume": True}, "non-finite OHLCV"),
        ({"volume": -1.0}, "negative OHLCV"),
        ({"high": 4_580.0}, "inconsistent OHLC"),
        ({"open": 4_600.0, "high": 4_595.0, "low": 4_500.0, "close": 4_590.0}, "inconsistent OHLC"),
        ({"high": 4_630.0}, "inconsistent OHLC"),
        ({"low": 4_610.0}, "inconsistent OHLC"),
        (
            {"open": 4_650.0, "high": 4_700.0, "low": 4_645.0, "close": 4_640.0},
            "inconsistent OHLC",
        ),
    ],
)
def test_normalize_rejects_invalid_bar_identity_range_and_ohlcv(
    changes: dict[str, object], message: str
) -> None:
    """Provider values retain a valid symbol, date, finite units, and OHLC shape."""
    invalid_bar = _bars()[0].model_copy(update=changes)
    with pytest.raises(ValueError, match=message):
        _job().normalize((invalid_bar,), _request(), _context())


def test_job_handlers_reject_requests_outside_the_fixed_backfill_contract() -> None:
    """Fetch and normalize cannot widen or change their job's fixed backfill."""
    job = _job()
    request = _request()
    invalid_requests = (
        RunRequest(
            run_type="manual",
            query={},
            partition={"symbol": SSE50_INDEX_SYMBOL},
            idempotency_key="fixture-manual-request-001",
        ),
        request.model_copy(update={"query": {"adjustment": "qfq"}}),
        request.model_copy(update={"partition": {"symbol": "000300.SH"}}),
        request.model_copy(update={"shard": "part-1"}),
        request.model_copy(update={"backfill_start": None}),
        request.model_copy(update={"backfill_end": None}),
    )
    for invalid_request in invalid_requests:
        with pytest.raises(ValueError, match="fixed-symbol index backfill"):
            job.fetch(invalid_request, _context())
        with pytest.raises(ValueError, match="fixed-symbol index backfill"):
            job.normalize((), invalid_request, _context())
    with pytest.raises(TypeError, match="request must be a RunRequest"):
        job.normalize((), object(), _context())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("job_id", "other-index-job"),
        ("version", "publication-v2"),
        ("dataset_id", "cn.csi300.index.daily"),
        ("provider", "other-provider"),
        ("model", "other-model"),
    ],
)
def test_publish_rejects_job_identity_mismatch_before_writing(field: str, value: str) -> None:
    """The publisher cannot route a batch through a different job identity."""
    job = _job()
    request = _request()
    batch = job.normalize(_bars(), request, _context())
    mismatched_task = job.task.model_copy(update={field: value})

    with pytest.raises(ValueError, match="does not match this single-index job"):
        job.publish(None, mismatched_task, request, batch, "fixture-mismatched-task-001")  # type: ignore[arg-type]


def test_publish_rejects_invalid_rows_and_contract_versions_before_writing() -> None:
    """Publication requires typed single-index rows and the exact contract versions."""
    job = _job()
    request = _request()
    batch = job.normalize(_bars(), request, _context())

    malformed_batches = (
        replace(batch, records=(object(),)),
        replace(batch, records=(replace(batch.records[0], payload=[]),)),  # type: ignore[arg-type]
        replace(batch, schema_version="index-ohlcv-v999"),
        replace(batch, semantic_version="unknown-semantics"),
    )
    expected_errors = (
        "invalid index record",
        "invalid index record",
        "does not match this single-index contract",
        "does not match this single-index contract",
    )
    for invalid_batch, message in zip(malformed_batches, expected_errors, strict=True):
        with pytest.raises(ValueError, match=message):
            job.publish(
                None,
                job.task,
                request,
                invalid_batch,
                "fixture-malformed-publication-001",
            )  # type: ignore[arg-type]

    wrong_request = request.model_copy(update={"partition": {"symbol": "000300.SH"}})
    with pytest.raises(ValueError, match="fixed-symbol index backfill"):
        job.publish(
            None,
            job.task,
            wrong_request,
            batch,
            "fixture-cross-dataset-request-001",
        )  # type: ignore[arg-type]
