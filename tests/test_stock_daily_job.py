"""Behavioral tests for the offline-reviewed stock daily pipeline adapter."""

from __future__ import annotations

import base64
import json
import os
import stat
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
import requests
from sqlalchemy import create_engine, select

import opendata.data.providers.akshare.stock_daily_wire as stock_daily_wire
import opendata.pipeline.stock_daily_job as stock_daily_job
from opendata.data.dataset_contract_registry import DatasetContractRegistry
from opendata.data.http_client import BoundedHttpResponse, GovernedHttpClient, HttpClientConfig
from opendata.data.platform_contracts import DatasetContract, FieldContract, SourceIdentity
from opendata.data.providers.akshare._source import SOURCE
from opendata.data.request_budget import (
    GrantDecision,
    RequestAttemptLimitError,
    RequestAuthorizationError,
    RequestBudget,
    RequestBudgetProfile,
    RequestBudgetScopeError,
    RequestGrant,
    RequestOperation,
    authorize_request,
    current_request_scopes,
    request_execution_scope,
    reserve_scoped_attempt,
)
from opendata.pipeline.publication_store import (
    PUBLICATION_METADATA,
    PUBLICATIONS,
    PUBLISHED_FACT_VERSIONS,
)
from opendata.pipeline.raw_evidence import (
    RawResponseArtifactWriter,
    RawResponseSource,
)
from opendata.pipeline.run_identity import RunRequest, ScheduleMetadata, TaskDefinition
from opendata.pipeline.run_ledger import RUN_LEDGER_METADATA, RunLedger
from opendata.pipeline.run_service import (
    ReviewedJobRegistry,
    RunExecutionContext,
    RunService,
)
from opendata.pipeline.stock_daily_job import (
    MAX_STOCK_DAILY_WINDOW_DAYS,
    CapturedStockDailyResponse,
    StockDailyJsonResponse,
    build_stock_daily_job,
    build_stock_daily_process_source,
)

UTC = timezone.utc
OBSERVED_AT = datetime(2026, 10, 27, 8, 15, tzinfo=UTC)
DATASET_ID = "cn-equity-stock-daily"
SCHEMA_VERSION = "1.0.0"
SEMANTIC_VERSION = "stock-daily-raw-v1"


def _contract() -> DatasetContract:
    """Build the caller-supplied explicit contract for CN unadjusted daily bars."""
    return DatasetContract(
        dataset_id=DATASET_ID,
        schema_version=SCHEMA_VERSION,
        semantic_version=SEMANTIC_VERSION,
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
        source=SourceIdentity(source_id="akshare.daily", source=SOURCE),
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


def _task(*, enabled: bool = False) -> TaskDefinition:
    """Return versioned task metadata, disabled unless a test enables it."""
    return TaskDefinition(
        job_id="cn-stock-daily-fixture",
        version="v1",
        dataset_id=DATASET_ID,
        provider=SOURCE,
        model="stock_daily",
        schedule=ScheduleMetadata(kind="cron", expression="0 16 * * mon-fri"),
        enabled=enabled,
    )


def _wire_body(*, symbol: str = "600519", market: int = 1) -> bytes:
    """Return a small synthetic Eastmoney envelope for an explicit fixture."""
    return json.dumps(
        {
            "rc": 0,
            "data": {
                "code": symbol,
                "market": market,
                "klines": [
                    "2026-10-26,10.0,11.0,11.5,9.5,12,134,20,10,1,2",
                    "2026-10-27,11.0,11.2,11.8,10.7,13,145,12,2,0.2,1",
                ],
            },
        },
        separators=(",", ":"),
    ).encode()


def _request(
    *,
    run_type: str = "manual",
    symbol: str = "600519.SH",
    market: str = "cn",
    start: str = "2026-10-26",
    end: str = "2026-10-27",
    shard: str = "600519",
    backfill_start: date | None = None,
    backfill_end: date | None = None,
) -> RunRequest:
    """Build a single-symbol request using the documented inclusive bounds."""
    values: dict[str, object] = {
        "run_type": run_type,
        "query": {
            "symbol": symbol,
            "market": market,
            "start_date": start,
            "end_date": end,
        },
        "partition": {},
        "shard": shard,
        "idempotency_key": "offline-stock-request-1",
    }
    if run_type == "backfill":
        values["backfill_start"] = backfill_start
        values["backfill_end"] = backfill_end
    return RunRequest.model_validate(values)


def _make_job(
    tmp_path: Path,
    source,
    *,
    contract: DatasetContract | None = None,
):
    """Create the disabled adapter with explicitly supplied fixture dependencies."""
    selected_contract = contract or _contract()
    return build_stock_daily_job(
        _task(),
        schema_version=SCHEMA_VERSION,
        semantic_version=SEMANTIC_VERSION,
        contract_registry=DatasetContractRegistry([selected_contract]),
        artifact_writer=RawResponseArtifactWriter(tmp_path / "private-raw"),
        response_source=source,
    )


def _context() -> RunExecutionContext:
    """Provide a deterministic no-op lease checkpoint for handler-level tests."""
    return RunExecutionContext(
        run_key="fixture-run-key",
        worker="fixture-worker",
        fencing_token=1,
        _checkpoint=lambda: None,
    )


def _make_private_artifact_root(root: Path) -> None:
    """Create the writer's fixed private root and managed directory layout."""
    root.mkdir(mode=0o700)
    (root / "objects").mkdir(mode=0o700)
    (root / "objects" / "pending").mkdir(mode=0o700)
    (root / "manifests").mkdir(mode=0o700)


def test_disabled_unreviewed_job_cannot_be_submitted(tmp_path: Path) -> None:
    """The factory keeps the task off and refuses execution until explicit review."""
    engine = create_engine("sqlite:///:memory:")
    RUN_LEDGER_METADATA.create_all(engine)
    ledger = RunLedger(engine)
    calls: list[str] = []

    def source(_query):
        calls.append("called")
        return StockDailyJsonResponse(_wire_body(), 200, OBSERVED_AT)

    job = _make_job(tmp_path, source)
    service = RunService(ledger, ReviewedJobRegistry([job]))
    assert job.task.enabled is False
    assert job.reviewed is False
    with pytest.raises(ValueError, match="not been reviewed"):
        service.submit(job.task.job_id, job.task.version, _request())
    assert calls == []
    engine.dispose()


def test_fetch_rejects_non_run_request_at_job_boundary(tmp_path: Path) -> None:
    """The fetch boundary rejects callers that bypass the request DTO."""
    job = _make_job(
        tmp_path,
        lambda _query: StockDailyJsonResponse(_wire_body(), 200, OBSERVED_AT),
    )
    with pytest.raises(TypeError, match="must be a RunRequest"):
        job.fetch(None, _context())


def test_factory_rejects_enabled_task_and_non_stock_contract(tmp_path: Path) -> None:
    """Activation metadata and a mismatched caller contract fail during setup."""

    def source(_query):
        return StockDailyJsonResponse(_wire_body(), 200, OBSERVED_AT)

    contracts = DatasetContractRegistry([_contract()])
    writer = RawResponseArtifactWriter(tmp_path / "private-raw")
    with pytest.raises(ValueError, match="must be disabled"):
        build_stock_daily_job(
            _task(enabled=True),
            schema_version=SCHEMA_VERSION,
            semantic_version=SEMANTIC_VERSION,
            contract_registry=contracts,
            artifact_writer=writer,
            response_source=source,
        )
    bad_contract = _contract().model_copy(update={"pit_policy": "required"})
    with pytest.raises(ValueError, match="temporal semantics"):
        build_stock_daily_job(
            _task(),
            schema_version=SCHEMA_VERSION,
            semantic_version=SEMANTIC_VERSION,
            contract_registry=DatasetContractRegistry([bad_contract]),
            artifact_writer=writer,
            response_source=source,
        )


@pytest.mark.parametrize(
    "contract_change",
    ["extra_field", "field_type", "identity", "units", "adjustment", "currency_map"],
)
def test_factory_rejects_incompatible_contract_semantics(
    tmp_path: Path,
    contract_change: str,
) -> None:
    """The injected schema must retain market identity and explicit units."""
    contract = _contract()
    if contract_change == "extra_field":
        contract = contract.model_copy(
            update={"fields": (*contract.fields, FieldContract(name="extra", data_type="string"))}
        )
    elif contract_change == "field_type":
        contract = contract.model_copy(
            update={
                "fields": tuple(
                    FieldContract(
                        name=field.name,
                        data_type="integer" if field.name == "market" else field.data_type,
                        nullable=field.nullable,
                    )
                    for field in contract.fields
                )
            }
        )
    elif contract_change == "identity":
        contract = contract.model_copy(update={"currency": "USD"})
    elif contract_change == "units":
        units = dict(contract.field_units)
        units["volume"] = "lots"
        contract = contract.model_copy(update={"field_units": units})
    elif contract_change == "adjustment":
        adjustments = dict(contract.field_adjustments)
        adjustments["volume"] = "raw"
        contract = contract.model_copy(update={"field_adjustments": adjustments})
    else:
        currencies = dict(contract.field_currencies)
        currencies.pop("amount")
        contract = contract.model_copy(update={"field_currencies": currencies})

    with pytest.raises(ValueError, match="stock daily contract"):
        _make_job(tmp_path, lambda _query: None, contract=contract)


@pytest.mark.parametrize(
    "invalid_kind",
    ["task_type", "provider", "model", "schema", "semantic", "registry", "writer", "source"],
)
def test_factory_rejects_invalid_task_or_missing_explicit_dependencies(
    tmp_path: Path,
    invalid_kind: str,
) -> None:
    """Every runtime dependency and version remains explicit at construction."""
    values: dict[str, object] = {
        "task": _task(),
        "schema_version": SCHEMA_VERSION,
        "semantic_version": SEMANTIC_VERSION,
        "contract_registry": DatasetContractRegistry([_contract()]),
        "artifact_writer": RawResponseArtifactWriter(tmp_path / "private-raw"),
        "response_source": lambda _query: StockDailyJsonResponse(_wire_body(), 200, OBSERVED_AT),
    }
    if invalid_kind == "task_type":
        values["task"] = None
        expected = "task must be a TaskDefinition"
    elif invalid_kind == "provider":
        values["task"] = _task().model_copy(update={"provider": "unreviewed-source"})
        expected = "registered akshare"
    elif invalid_kind == "model":
        values["task"] = _task().model_copy(update={"model": "arbitrary-module.path"})
        expected = "registered akshare"
    elif invalid_kind == "schema":
        values["schema_version"] = ""
        expected = "schema_version"
    elif invalid_kind == "semantic":
        values["semantic_version"] = ""
        expected = "semantic_version"
    elif invalid_kind == "registry":
        values["contract_registry"] = object()
        expected = "contract_registry"
    elif invalid_kind == "writer":
        values["artifact_writer"] = object()
        expected = "artifact_writer"
    else:
        values["response_source"] = None
        expected = "response_source"

    with pytest.raises((TypeError, ValueError), match=expected):
        build_stock_daily_job(**values)


def test_factory_rejects_unknown_contract_version_and_invalid_task_graph(tmp_path: Path) -> None:
    """Missing exact registry entries and model_construct bypasses fail closed."""
    writer = RawResponseArtifactWriter(tmp_path / "private-raw")

    def response_source(_query):
        return StockDailyJsonResponse(_wire_body(), 200, OBSERVED_AT)

    with pytest.raises(ValueError, match="exact stock daily contract"):
        build_stock_daily_job(
            _task(),
            schema_version="missing",
            semantic_version=SEMANTIC_VERSION,
            contract_registry=DatasetContractRegistry([_contract()]),
            artifact_writer=writer,
            response_source=response_source,
        )
    invalid_task = TaskDefinition.model_construct(job_id="bypassed")
    with pytest.raises(ValueError, match="task metadata failed"):
        build_stock_daily_job(
            invalid_task,
            schema_version=SCHEMA_VERSION,
            semantic_version=SEMANTIC_VERSION,
            contract_registry=DatasetContractRegistry([_contract()]),
            artifact_writer=writer,
            response_source=response_source,
        )


def test_stock_contract_validator_has_a_runtime_type_guard() -> None:
    """A malformed resolved object cannot bypass the explicit contract checks."""
    # DatasetContractRegistry always returns a validated DatasetContract. The
    # private validator still has a fail-closed type guard for future adapters.
    with pytest.raises(ValueError, match="invalid dataset contract"):
        stock_daily_job._validate_stock_contract(_task(), object())


def test_run_service_publishes_verified_raw_bars_with_market_and_observation_time(
    tmp_path: Path,
) -> None:
    """The fixture executes only after explicit review and publishes exact units."""
    engine = create_engine(f"sqlite:///{tmp_path / 'stock.sqlite'}")
    RUN_LEDGER_METADATA.create_all(engine)
    PUBLICATION_METADATA.create_all(engine)
    ledger = RunLedger(engine)
    seen_queries: list[tuple[str, str, date, date]] = []

    def source(query):
        seen_queries.append((query.symbol, query.market, query.start_date, query.end_date))
        return StockDailyJsonResponse(_wire_body(), 200, OBSERVED_AT)

    unreviewed = _make_job(tmp_path, source)
    reviewed = replace(
        unreviewed,
        task=_task(enabled=True),
        reviewed=True,
    )
    service = RunService(
        ledger,
        ReviewedJobRegistry([reviewed]),
        clock=lambda: datetime(2026, 10, 27, 9, tzinfo=UTC),
    )
    queued = service.submit(reviewed.task.job_id, reviewed.task.version, _request())
    outcome = service.run_once("offline-stock-worker", timedelta(minutes=2))
    assert outcome is not None
    assert outcome.record is not None
    assert outcome.record.run_key == queued.run_key
    assert outcome.record.status == "succeeded", outcome.record.error_text
    assert outcome.publication is not None
    assert outcome.publication.record_count == 2
    assert seen_queries == [("600519", "cn", date(2026, 10, 26), date(2026, 10, 27))]

    with engine.connect() as connection:
        facts = (
            connection.execute(
                select(PUBLISHED_FACT_VERSIONS).order_by(PUBLISHED_FACT_VERSIONS.c.business_key)
            )
            .mappings()
            .all()
        )
        publication = (
            connection.execute(
                select(PUBLICATIONS).where(
                    PUBLICATIONS.c.publication_id == outcome.publication.publication_id
                )
            )
            .mappings()
            .one()
        )
    first = facts[0]
    payload = json.loads(first["record_json"])
    provenance = json.loads(publication["provenance_json"])
    assert first["business_key"] == '["cn","600519","2026-10-26"]'
    assert first["business_date"] == date(2026, 10, 26)
    assert first["observed_at"] == "2026-10-27T08:15:00.000000Z"
    assert payload == {
        "amount": 134.0,
        "close": 11.0,
        "high": 11.5,
        "low": 9.5,
        "market": "cn",
        "open": 10.0,
        "symbol": "600519",
        "trade_date": "2026-10-26",
        "volume": 1200.0,
    }
    assert provenance["source_verification"] == "not_performed"
    assert provenance["source_history_limit"] == "unknown"
    assert len(provenance["source_response_sha256"]) == 64
    assert len(provenance["source_manifest_sha256"]) == 64
    assert json.loads(publication["quality_json"]) == {
        "status": "warning",
        "report_id": "stock-daily-offline-normalization-v1",
        "issue_count": 0,
    }
    engine.dispose()


def test_normalize_rejects_capture_replayed_for_different_window(tmp_path: Path) -> None:
    """Captured evidence is bound to the exact canonical request window."""
    job = _make_job(
        tmp_path,
        lambda _query: StockDailyJsonResponse(_wire_body(), 200, OBSERVED_AT),
    )
    captured = job.fetch(_request(), _context())
    wider_request = _request(end="2026-10-28")
    with pytest.raises(ValueError, match="does not match the current normalized query"):
        job.normalize(captured, wider_request, _context())


@pytest.mark.parametrize(
    ("market", "shard", "start", "end"),
    [
        ("us", "600519", "2026-10-26", "2026-10-27"),
        ("cn", "600000", "2026-10-26", "2026-10-27"),
        ("cn", "600519", "2026-10-27", "2026-10-26"),
        ("cn", "600519", "", "2026-10-27"),
        ("cn", "600519", "2026-10-26", ""),
    ],
)
def test_invalid_market_symbol_shard_or_window_fails_before_source_call(
    tmp_path: Path,
    market: str,
    shard: str,
    start: str,
    end: str,
) -> None:
    """Untrusted run query fields cannot widen or redirect this fixed job."""
    calls: list[str] = []

    def source(_query):
        calls.append("called")
        return StockDailyJsonResponse(_wire_body(), 200, OBSERVED_AT)

    job = _make_job(tmp_path, source)
    request = _request(market=market, shard=shard, start=start, end=end)
    with pytest.raises((ValueError, TypeError)):
        job.fetch(request, _context())
    assert calls == []


def test_backfill_requires_request_range_to_match_inclusive_source_window(tmp_path: Path) -> None:
    """Backfill metadata cannot imply a range different from the queried dates."""
    calls: list[str] = []

    def source(_query):
        calls.append("called")
        return StockDailyJsonResponse(_wire_body(), 200, OBSERVED_AT)

    job = _make_job(tmp_path, source)
    request = _request(
        run_type="backfill",
        backfill_start=date(2026, 10, 25),
        backfill_end=date(2026, 10, 27),
    )
    with pytest.raises(ValueError, match="backfill bounds"):
        job.fetch(request, _context())
    assert calls == []


def test_query_revalidation_rejects_unknown_fields_missing_dates_and_bypassed_models(
    tmp_path: Path,
) -> None:
    """Query JSON cannot add ignored fields or rely on provider-wide defaults."""
    calls: list[str] = []

    def source(_query):
        calls.append("called")
        return StockDailyJsonResponse(_wire_body(), 200, OBSERVED_AT)

    job = _make_job(tmp_path, source)
    extra_payload = _request().model_dump(mode="python")
    extra_payload["query"]["sql"] = "DELETE FROM anything"
    missing_date_payload = _request().model_dump(mode="python")
    missing_date_payload["query"].pop("start_date")
    invalid_request = RunRequest.model_construct(run_type="manual", query={"symbol": "600519"})
    for invalid in (
        RunRequest.model_validate(extra_payload),
        RunRequest.model_validate(missing_date_payload),
        invalid_request,
    ):
        with pytest.raises(ValueError):
            job.fetch(invalid, _context())
    assert calls == []


def test_window_has_a_fixed_maximum_and_rejects_partitions(tmp_path: Path) -> None:
    """One run is bounded to at most ten years and cannot add ignored partitions."""
    calls: list[str] = []

    def source(_query):
        calls.append("called")
        return StockDailyJsonResponse(_wire_body(), 200, OBSERVED_AT)

    job = _make_job(tmp_path, source)
    wide_request = _request(start="1990-01-01", end="2010-01-01")
    with pytest.raises(ValueError, match="date-range limit"):
        job.fetch(wide_request, _context())
    partitioned = _request().model_copy(update={"partition": {"date": "2026-10-26"}})
    with pytest.raises(ValueError, match="extra partitions"):
        job.fetch(partitioned, _context())
    assert MAX_STOCK_DAILY_WINDOW_DAYS == 3_660
    assert calls == []


@pytest.mark.parametrize(
    ("status", "body", "observed_at"),
    [
        (503, _wire_body(), OBSERVED_AT),
        (200, _wire_body() + b" " * (2 * 1024 * 1024), OBSERVED_AT),
        (200, _wire_body(), datetime(2026, 10, 27, 8, 15)),
    ],
)
def test_invalid_source_response_is_rejected_before_capture(
    tmp_path: Path,
    status: int,
    body: bytes,
    observed_at: datetime,
) -> None:
    """Status, body-size, and observation-time errors never create evidence."""
    job = _make_job(
        tmp_path,
        lambda _query: StockDailyJsonResponse(body, status, observed_at),
    )
    with pytest.raises(ValueError):
        job.fetch(_request(), _context())
    assert list((tmp_path / "private-raw").glob("objects/*/*.blob")) == []


def test_source_body_must_be_immutable_bytes_and_response_type_is_exact(tmp_path: Path) -> None:
    """Mutable buffers are rejected before the artifact writer sees source data."""
    mutable_body = StockDailyJsonResponse(bytearray(_wire_body()), 200, OBSERVED_AT)
    job = _make_job(tmp_path, lambda _query: mutable_body)
    with pytest.raises(ValueError, match="immutable bytes"):
        job.fetch(_request(), _context())
    assert not list((tmp_path / "private-raw").glob("objects/*/*.blob"))


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (_wire_body(symbol="600000"), "code does not exactly match"),
        (_wire_body(market=0), "market does not match"),
        (b"{not-json", "not strict JSON"),
    ],
)
def test_wire_identity_and_strict_json_errors_fail_normalization(
    tmp_path: Path,
    body: bytes,
    message: str,
) -> None:
    """Source market and symbol identities remain checked after evidence capture."""
    job = _make_job(
        tmp_path,
        lambda _query: StockDailyJsonResponse(body, 200, OBSERVED_AT),
    )
    captured = job.fetch(_request(), _context())
    with pytest.raises(ValueError, match=message):
        job.normalize(captured, _request(), _context())


@pytest.mark.parametrize("target", ["body", "manifest"])
def test_raw_artifact_tampering_fails_hash_verification(tmp_path: Path, target: str) -> None:
    """Both response bytes and canonical manifest bytes are re-hashed at normalize."""
    job = _make_job(
        tmp_path,
        lambda _query: StockDailyJsonResponse(_wire_body(), 200, OBSERVED_AT),
    )
    captured = job.fetch(_request(), _context())
    artifact_path = (
        captured.capture.artifact_path if target == "body" else captured.capture.manifest_path
    )
    artifact_path.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="failed canonical hash verification|failed hash"):
        job.normalize(captured, _request(), _context())


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("manifest_type", "manifest has an invalid type"),
        ("manifest_schema", "manifest failed boundary validation"),
        ("source_identity", "source identity or HTTP status changed"),
        ("status", "source identity or HTTP status changed"),
        ("response_size", "body exceeds the wire response limit"),
        ("body_path", "path does not match its content address"),
        ("manifest_path", "path does not match its content address"),
    ],
)
def test_capture_metadata_and_paths_are_revalidated_before_read(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    """Captured dataclasses and their nested manifest cannot bypass validation."""
    job = _make_job(
        tmp_path,
        lambda _query: StockDailyJsonResponse(_wire_body(), 200, OBSERVED_AT),
    )
    captured = job.fetch(_request(), _context())
    raw_capture = captured.capture
    if mutation == "manifest_type":
        raw_capture = replace(raw_capture, manifest=object())
    elif mutation == "manifest_schema":
        raw_capture = replace(
            raw_capture,
            manifest=raw_capture.manifest.model_copy(update={"http_status": "200"}),
        )
    elif mutation == "source_identity":
        wrong_source = RawResponseSource(
            provider_id="other-source",
            dataset_id=DATASET_ID,
            operation_id="stock-daily-wire",
        )
        raw_capture = replace(
            raw_capture,
            manifest=raw_capture.manifest.model_copy(update={"source": wrong_source}),
        )
    elif mutation == "status":
        raw_capture = replace(
            raw_capture,
            manifest=raw_capture.manifest.model_copy(update={"http_status": 201}),
        )
    elif mutation == "response_size":
        raw_capture = replace(
            raw_capture,
            manifest=raw_capture.manifest.model_copy(
                update={"response_size_bytes": 2 * 1024 * 1024 + 1}
            ),
        )
    elif mutation == "body_path":
        raw_capture = replace(raw_capture, artifact_path=tmp_path / "wrong.blob")
    else:
        raw_capture = replace(raw_capture, manifest_path=tmp_path / "wrong.json")
    mutated = CapturedStockDailyResponse(capture=raw_capture, observed_at=captured.observed_at)
    if mutation == "manifest_schema":
        # Pydantic reports the deliberately forged model field before strict
        # boundary validation rejects it.
        with (
            pytest.warns(UserWarning, match="Pydantic serializer warnings"),
            pytest.raises((TypeError, ValueError), match=message),
        ):
            job.normalize(mutated, _request(), _context())
    else:
        with pytest.raises((TypeError, ValueError), match=message):
            job.normalize(mutated, _request(), _context())


def test_manifest_canonical_size_limit_is_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A changed manifest serialization cannot exceed the independent cap."""
    job = _make_job(
        tmp_path,
        lambda _query: StockDailyJsonResponse(_wire_body(), 200, OBSERVED_AT),
    )
    captured = job.fetch(_request(), _context())
    monkeypatch.setattr(stock_daily_job, "MAX_MANIFEST_BYTES", 1)
    with pytest.raises(ValueError, match="manifest exceeds its size limit"):
        job.normalize(captured, _request(), _context())


def test_response_source_must_return_fixed_response_type(tmp_path: Path) -> None:
    """A dict cannot select imports or pass untyped content around the adapter."""
    job = _make_job(tmp_path, lambda _query: {"body": _wire_body()})
    with pytest.raises(TypeError, match="StockDailyJsonResponse"):
        job.fetch(_request(), _context())


def test_normalizer_rejects_non_capture_and_non_bar_results(tmp_path: Path, monkeypatch) -> None:
    """Normalization only accepts captured evidence and the provider Bar model."""
    job = _make_job(
        tmp_path,
        lambda _query: StockDailyJsonResponse(_wire_body(), 200, OBSERVED_AT),
    )
    with pytest.raises(ValueError, match="requires its captured raw response"):
        job.normalize(object(), _request(), _context())
    forged_capture = CapturedStockDailyResponse(capture=object(), observed_at=OBSERVED_AT)
    with pytest.raises(TypeError, match="CapturedRawResponse"):
        job.normalize(forged_capture, _request(), _context())
    captured = job.fetch(_request(), _context())
    monkeypatch.setattr(
        stock_daily_job.AkshareStockDailyFetcher,
        "transform_data",
        lambda _self, _frame, _query: [object()],
    )
    with pytest.raises(ValueError, match="non-Bar record"):
        job.normalize(captured, _request(), _context())


def test_capture_reader_rejects_symlink_fifo_missing_and_unsafe_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Evidence reads are bounded regular-file reads through no-follow handles."""
    root = tmp_path / "managed"
    _make_private_artifact_root(root)
    regular = root / "regular.bin"
    regular.write_bytes(b"ok")
    regular.chmod(0o600)
    assert stock_daily_job._read_private_managed_file(root, ("regular.bin",), maximum=2) == b"ok"
    with pytest.raises(ValueError, match="absolute"):
        stock_daily_job._read_private_managed_file(Path("relative"), ("x",), maximum=1)
    with pytest.raises(ValueError, match="unsafe component"):
        stock_daily_job._read_private_managed_file(root, ("..", "x"), maximum=1)
    with pytest.raises(ValueError, match="unsafe component"):
        stock_daily_job._read_private_managed_file(root / ".." / "other", ("x",), maximum=1)
    with pytest.raises(ValueError, match="unavailable or unsafe"):
        stock_daily_job._read_private_managed_file(root, ("missing.bin",), maximum=1)
    too_large = root / "too-large.bin"
    too_large.write_bytes(b"long")
    with pytest.raises(ValueError, match="bounded regular file"):
        stock_daily_job._read_private_managed_file(root, ("too-large.bin",), maximum=2)
    fifo = root / "pipe"
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match="bounded regular file"):
        stock_daily_job._read_private_managed_file(root, ("pipe",), maximum=10)
    target = root / "target.bin"
    target.write_bytes(b"target")
    link = root / "link.bin"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="unavailable or unsafe"):
        stock_daily_job._read_private_managed_file(root, ("link.bin",), maximum=10)
    monkeypatch.delattr(stock_daily_job.os, "O_NOFOLLOW")
    with pytest.raises(ValueError, match="cannot safely verify"):
        stock_daily_job._read_private_managed_file(root, ("regular.bin",), maximum=10)


@pytest.mark.parametrize(
    ("target", "mode", "message"),
    [
        ("root", 0o755, "managed artifact directory must have mode 0700"),
        ("objects", 0o755, "managed artifact directory must have mode 0700"),
        ("pending", 0o755, "managed artifact directory must have mode 0700"),
        ("manifests", 0o755, "managed artifact directory must have mode 0700"),
        ("body_prefix", 0o755, "managed artifact directory must have mode 0700"),
        ("manifest_prefix", 0o755, "managed artifact directory must have mode 0700"),
        ("body", 0o644, "managed artifact file must have mode 0600"),
        ("manifest", 0o644, "managed artifact file must have mode 0600"),
    ],
)
def test_normalize_rejects_public_artifact_modes(
    tmp_path: Path,
    target: str,
    mode: int,
    message: str,
) -> None:
    """Every managed directory and artifact file remains owner-private."""
    job = _make_job(
        tmp_path,
        lambda _query: StockDailyJsonResponse(_wire_body(), 200, OBSERVED_AT),
    )
    captured = job.fetch(_request(), _context())
    artifact_root = tmp_path / "private-raw"
    targets = {
        "root": artifact_root,
        "objects": artifact_root / "objects",
        "pending": artifact_root / "objects" / "pending",
        "manifests": artifact_root / "manifests",
        "body_prefix": captured.capture.artifact_path.parent,
        "manifest_prefix": captured.capture.manifest_path.parent,
        "body": captured.capture.artifact_path,
        "manifest": captured.capture.manifest_path,
    }
    targets[target].chmod(mode)
    with pytest.raises(ValueError, match=message):
        job.normalize(captured, _request(), _context())


def test_normalize_rejects_symlinked_managed_directory(tmp_path: Path) -> None:
    """Managed directory opens stay anchored and refuse symlink substitutions."""
    job = _make_job(
        tmp_path,
        lambda _query: StockDailyJsonResponse(_wire_body(), 200, OBSERVED_AT),
    )
    captured = job.fetch(_request(), _context())
    pending = tmp_path / "private-raw" / "objects" / "pending"
    external = tmp_path / "external-pending"
    external.mkdir(mode=0o700)
    pending.rmdir()
    pending.symlink_to(external, target_is_directory=True)
    with pytest.raises(ValueError, match="unavailable or unsafe"):
        job.normalize(captured, _request(), _context())
    assert list(external.iterdir()) == []


def test_normalize_rejects_artifacts_owned_by_another_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The descriptor checks bind the private artifact chain to the current user."""
    job = _make_job(
        tmp_path,
        lambda _query: StockDailyJsonResponse(_wire_body(), 200, OBSERVED_AT),
    )
    captured = job.fetch(_request(), _context())
    current_uid = os.geteuid()
    monkeypatch.setattr(stock_daily_job.os, "geteuid", lambda: current_uid + 1)
    with pytest.raises(ValueError, match="must be owned by the current user"):
        job.normalize(captured, _request(), _context())


def test_normalize_rejects_artifact_file_owned_by_another_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Leaf-file ownership is checked on the opened file descriptor itself."""
    job = _make_job(
        tmp_path,
        lambda _query: StockDailyJsonResponse(_wire_body(), 200, OBSERVED_AT),
    )
    captured = job.fetch(_request(), _context())
    current_uid = os.geteuid()
    real_fstat = os.fstat

    def report_other_file_owner(descriptor: int) -> os.stat_result | SimpleNamespace:
        info = real_fstat(descriptor)
        if stat.S_ISREG(info.st_mode):
            return SimpleNamespace(
                st_mode=info.st_mode,
                st_size=info.st_size,
                st_uid=current_uid + 1,
            )
        return info

    monkeypatch.setattr(stock_daily_job.os, "fstat", report_other_file_owner)
    with pytest.raises(ValueError, match="must be owned by the current user"):
        job.normalize(captured, _request(), _context())


def test_capture_reader_detects_in_place_file_growth(tmp_path: Path, monkeypatch) -> None:
    """The file descriptor size is rechecked after the bounded read completes."""
    root = tmp_path / "managed"
    _make_private_artifact_root(root)
    content = root / "content.bin"
    content.write_bytes(b"before")
    content.chmod(0o600)
    real_read = os.read
    did_append = False

    def append_after_read(descriptor: int, count: int) -> bytes:
        nonlocal did_append
        chunk = real_read(descriptor, count)
        if not did_append:
            with content.open("ab") as stream:
                stream.write(b"after")
            did_append = True
        return chunk

    monkeypatch.setattr(stock_daily_job.os, "read", append_after_read)
    with pytest.raises(ValueError, match="changed or exceeded"):
        stock_daily_job._read_private_managed_file(root, ("content.bin",), maximum=20)


def test_capture_reader_stops_after_growth_crosses_limit(tmp_path: Path, monkeypatch) -> None:
    """The bounded read loop stops once a concurrently grown file exceeds its cap."""
    root = tmp_path / "managed"
    _make_private_artifact_root(root)
    content = root / "content.bin"
    content.write_bytes(b"")
    content.chmod(0o600)
    real_read = os.read

    def append_before_read(descriptor: int, count: int) -> bytes:
        with content.open("ab") as stream:
            stream.write(b"abc")
        monkeypatch.setattr(stock_daily_job.os, "read", real_read)
        return real_read(descriptor, count)

    monkeypatch.setattr(stock_daily_job.os, "read", append_before_read)
    with pytest.raises(ValueError, match="changed or exceeded"):
        stock_daily_job._read_private_managed_file(root, ("content.bin",), maximum=2)


def _stock_daily_process_parameters() -> dict[str, str]:
    """Return the exact fixed child IPC query used by focused transport tests."""
    return {"symbol": "600519", "start_date": "20261026", "end_date": "20261027"}


def _synthetic_stock_daily_budget() -> RequestBudget:
    """Create explicit test-only authority for the synthetic governed client."""
    profile = RequestBudgetProfile.BOUNDED_REQUEST
    grant = RequestGrant(
        source=SOURCE,
        canonical_model="stock_daily",
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="synthetic test-only grant; no real source rights asserted",
        task_attempts=1,
        source_attempts=1,
        allowed_hosts={stock_daily_wire.EASTMONEY_KLINE_HOST},
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        profile=profile,
    )
    return RequestBudget(
        task_attempts=1,
        source_attempts=1,
        grants=(grant,),
        profile=profile,
    )


class _SyntheticGovernedStockClient:
    """Record a governed request and count only authority-approved fake sends."""

    def __init__(self, response: object) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.scopes = []
        self.transport_calls = 0

    def get_bounded(self, url: str, **kwargs: object) -> object:
        self.calls.append((url, dict(kwargs)))
        scopes = current_request_scopes()
        self.scopes.append(scopes)
        host = urlsplit(url).hostname
        assert host is not None
        source = kwargs.get("source")
        assert isinstance(source, str)
        authorize_request(scopes, source, host)
        reserve_scoped_attempt(scopes, source, host)
        self.transport_calls += 1
        return self.response


def test_stock_daily_process_worker_fails_closed_before_transport_for_unknown_rights(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real bounded client rejects UNKNOWN authority before Session.request."""
    send_calls: list[tuple[object, ...]] = []

    def spy_request(
        session: requests.Session,
        *args: object,
        **kwargs: object,
    ) -> requests.Response:
        send_calls.append((session, *args, kwargs))
        raise AssertionError("UNKNOWN authority must stop before Session.request")

    session = requests.Session()
    client = GovernedHttpClient(
        HttpClientConfig(rate_limit_per_host=None, max_attempts=1),
        session=session,
        raw_response_cache=None,
    )
    monkeypatch.setattr(requests.Session, "request", spy_request)
    monkeypatch.setattr(stock_daily_wire, "get_shared_http_client", lambda: client)

    with pytest.raises(RequestAuthorizationError, match="not explicitly ALLOWED"):
        stock_daily_wire.fetch_stock_daily_process(_stock_daily_process_parameters())

    assert send_calls == []


def test_stock_daily_process_worker_does_not_inherit_parent_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ambient ALLOWED scope cannot replace the worker's fresh child scope."""
    client = _SyntheticGovernedStockClient(BoundedHttpResponse(b"unused", 200, OBSERVED_AT))
    monkeypatch.setattr(stock_daily_wire, "get_shared_http_client", lambda: client)
    parent_budget = _synthetic_stock_daily_budget()

    with (
        request_execution_scope(
            source=SOURCE,
            canonical_model="stock_daily",
            operation=RequestOperation.QUERY,
            budget=parent_budget,
        ),
        pytest.raises(RequestBudgetScopeError, match="inheritance is inconsistent"),
    ):
        stock_daily_wire.fetch_stock_daily_process(_stock_daily_process_parameters())

    assert client.calls == []
    assert client.transport_calls == 0


def test_synthetic_stock_daily_grant_allows_only_one_attempt() -> None:
    """The child-local quota blocks a second transport attempt in one scope."""
    budget = _synthetic_stock_daily_budget()
    with request_execution_scope(
        source=SOURCE,
        canonical_model="stock_daily",
        operation=RequestOperation.QUERY,
        budget=budget,
    ):
        scopes = current_request_scopes()
        reserve_scoped_attempt(scopes, SOURCE, stock_daily_wire.EASTMONEY_KLINE_HOST)
        with pytest.raises(RequestAttemptLimitError, match="allowance exhausted"):
            reserve_scoped_attempt(scopes, SOURCE, stock_daily_wire.EASTMONEY_KLINE_HOST)


def test_stock_daily_process_worker_uses_fixed_query_and_bounded_http_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Trusted test code can inject a synthetic grant to inspect fixed HTTP identity."""
    body = _wire_body()
    retrieved_at = datetime(2026, 10, 27, 16, 15, tzinfo=timezone(timedelta(hours=8)))
    client = _SyntheticGovernedStockClient(BoundedHttpResponse(body, 200, retrieved_at))
    monkeypatch.setattr(
        stock_daily_wire, "_new_child_request_budget", _synthetic_stock_daily_budget
    )
    monkeypatch.setattr(stock_daily_wire, "get_shared_http_client", lambda: client)

    envelope = stock_daily_wire.fetch_stock_daily_process(_stock_daily_process_parameters())

    assert envelope == {
        "body_b64": base64.b64encode(body).decode("ascii"),
        "status_code": 200,
        "retrieved_at": "2026-10-27T08:15:00.000000Z",
    }
    assert len(client.calls) == 1
    url, options = client.calls[0]
    assert url == stock_daily_wire.EASTMONEY_KLINE_URL
    assert options == {
        "params": {
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "klt": "101",
            "fqt": "0",
            "secid": "1.600519",
            "beg": "20261026",
            "end": "20261027",
        },
        "max_body_bytes": 2 * 1024 * 1024,
        "overall_timeout": 20.0,
        "timeout": (3.0, 5.0),
        "source": SOURCE,
    }
    assert client.transport_calls == 1


def test_stock_daily_process_parameter_builder_revalidates_canonical_identity() -> None:
    """Only the canonical symbol and inclusive compact dates cross the process boundary."""
    assert stock_daily_job._stock_daily_process_parameters(_request()) == {
        "symbol": "600519",
        "start_date": "20261026",
        "end_date": "20261027",
    }


def test_stock_daily_process_validator_accepts_exact_maximum_inclusive_window() -> None:
    """The fixed child accepts exactly 3,660 dates and rejects one additional day."""
    start = date(2000, 1, 1)
    end = start + timedelta(days=stock_daily_wire.MAX_STOCK_DAILY_WINDOW_DAYS - 1)
    parameters = {
        "symbol": "600519",
        "start_date": start.strftime("%Y%m%d"),
        "end_date": end.strftime("%Y%m%d"),
    }

    assert stock_daily_wire._validate_process_parameters(parameters) == (
        "600519",
        parameters["start_date"],
        parameters["end_date"],
    )
    too_long = {
        **parameters,
        "end_date": (end + timedelta(days=1)).strftime("%Y%m%d"),
    }
    with pytest.raises(ValueError, match="query_invalid"):
        stock_daily_wire._validate_process_parameters(too_long)


@pytest.mark.parametrize(
    "parameters",
    [
        {
            "symbol": "600519",
            "start_date": "20261026",
            "end_date": "20261027",
            "url": "https://example.test",
        },
        {"symbol": "600519.SH", "start_date": "20261026", "end_date": "20261027"},
        {"symbol": "600519", "start_date": "2026-10-26", "end_date": "20261027"},
        {"symbol": "600519", "start_date": "20260230", "end_date": "20260301"},
        {"symbol": "600519", "start_date": "20261027", "end_date": "20261026"},
        {"symbol": "600519", "start_date": "20100101", "end_date": "20201231"},
    ],
)
def test_stock_daily_process_worker_rejects_untrusted_or_invalid_query(
    parameters: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only canonical symbols and strict inclusive windows enter the fixed worker."""
    client = _SyntheticGovernedStockClient(BoundedHttpResponse(b"unused", 200, OBSERVED_AT))
    monkeypatch.setattr(stock_daily_wire, "get_shared_http_client", lambda: client)

    with pytest.raises(ValueError):
        stock_daily_wire.fetch_stock_daily_process(parameters)

    assert client.calls == []
    assert client.transport_calls == 0


def test_stock_daily_process_worker_never_forges_non_200_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-200 successful transport envelope is rejected without a body result."""
    client = _SyntheticGovernedStockClient(BoundedHttpResponse(b"raw", 201, OBSERVED_AT))
    monkeypatch.setattr(
        stock_daily_wire, "_new_child_request_budget", _synthetic_stock_daily_budget
    )
    monkeypatch.setattr(stock_daily_wire, "get_shared_http_client", lambda: client)

    with pytest.raises(ValueError, match="http_status_invalid"):
        stock_daily_wire.fetch_stock_daily_process(_stock_daily_process_parameters())

    assert client.transport_calls == 1


@pytest.mark.parametrize(
    ("response", "error_code"),
    [
        (object(), "response_invalid"),
        (BoundedHttpResponse("not-bytes", 200, OBSERVED_AT), "body_limit_exceeded"),
        (
            BoundedHttpResponse(
                b"x" * (stock_daily_wire.MAX_RAW_RESPONSE_BYTES + 1), 200, OBSERVED_AT
            ),
            "body_limit_exceeded",
        ),
        (
            BoundedHttpResponse(b"raw", 200, datetime(2026, 10, 27, 8, 15)),
            "retrieved_at_invalid",
        ),
    ],
)
def test_stock_daily_process_worker_rejects_malformed_bounded_http_response(
    response: object,
    error_code: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed fake HTTP values yield no child envelope for parent capture."""
    client = _SyntheticGovernedStockClient(response)
    monkeypatch.setattr(
        stock_daily_wire, "_new_child_request_budget", _synthetic_stock_daily_budget
    )
    monkeypatch.setattr(stock_daily_wire, "get_shared_http_client", lambda: client)
    child_result: dict[str, str | int] | None = None

    with pytest.raises(ValueError, match=error_code):
        child_result = stock_daily_wire.fetch_stock_daily_process(_stock_daily_process_parameters())

    assert child_result is None
    assert client.transport_calls == 1


@pytest.mark.parametrize(
    "update",
    [
        {"status_code": True},
        {"status_code": 201},
        {"body_b64": "%%%"},
        {"body_b64": "YQ==="},
        {"retrieved_at": "2026-10-27T08:15:00"},
        {"retrieved_at": "2026-10-27T16:15:00+08:00"},
        {"retrieved_at": "not-a-timestamp"},
    ],
)
def test_stock_daily_process_decoder_rejects_malformed_ipc(
    update: dict[str, object],
) -> None:
    """The parent rejects malformed shape, status, Base64, and time fields."""
    envelope: dict[str, object] = {
        "body_b64": base64.b64encode(_wire_body()).decode("ascii"),
        "status_code": 200,
        "retrieved_at": OBSERVED_AT.isoformat().replace("+00:00", "Z"),
    }
    envelope.update(update)

    with pytest.raises(ValueError):
        stock_daily_job._decode_stock_daily_process_result(envelope)


def test_stock_daily_process_decoder_requires_exact_result_keys() -> None:
    """The parent rejects missing and additional child result keys."""
    valid: dict[str, object] = {
        "body_b64": base64.b64encode(_wire_body()).decode("ascii"),
        "status_code": 200,
        "retrieved_at": OBSERVED_AT.isoformat().replace("+00:00", "Z"),
    }
    with pytest.raises(ValueError, match="shape"):
        stock_daily_job._decode_stock_daily_process_result({"status_code": 200})
    with pytest.raises(ValueError, match="shape"):
        stock_daily_job._decode_stock_daily_process_result({**valid, "body": "not allowed"})


def test_stock_daily_process_base64_boundary_and_executor_result_budget() -> None:
    """A 2 MiB body fits the exact expanded envelope, while one extra byte fails."""
    maximum_body = b"x" * stock_daily_wire.MAX_RAW_RESPONSE_BYTES
    encoded_body = base64.b64encode(maximum_body).decode("ascii")
    maximum_envelope: dict[str, object] = {
        "body_b64": encoded_body,
        "status_code": 200,
        "retrieved_at": "9999-12-31T23:59:59.999999Z",
    }
    serialized = json.dumps(
        maximum_envelope,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert len(encoded_body) == stock_daily_wire.MAX_BASE64_RESPONSE_BYTES
    assert len(serialized) == stock_daily_wire.MAX_PROCESS_RESULT_JSON_BYTES
    decoded = stock_daily_job._decode_stock_daily_process_result(maximum_envelope)
    assert decoded.body == maximum_body

    too_large = {
        **maximum_envelope,
        "body_b64": base64.b64encode(maximum_body + b"x").decode("ascii"),
    }
    with pytest.raises(ValueError):
        stock_daily_job._decode_stock_daily_process_result(too_large)


def test_stock_daily_process_source_requires_capacity_for_expanded_envelope() -> None:
    """The source factory rejects executors whose result cap truncates Base64 IPC."""
    from opendata.pipeline.process_executor import ReviewedProcessExecutor, ReviewedProcessRegistry

    executor = ReviewedProcessExecutor(
        ReviewedProcessRegistry(
            {
                stock_daily_wire.STOCK_DAILY_PROCESS_OPERATION_ID: (
                    stock_daily_wire.fetch_stock_daily_process
                )
            }
        ),
        max_result_bytes=stock_daily_wire.MAX_PROCESS_RESULT_JSON_BYTES - 1,
    )
    with pytest.raises(ValueError, match="result cap"):
        build_stock_daily_process_source(executor)


def test_stock_daily_process_source_requires_capacity_for_fixed_request() -> None:
    """The executor parameter cap must carry the exact canonical query object."""
    from opendata.pipeline.process_executor import ReviewedProcessExecutor, ReviewedProcessRegistry

    executor = ReviewedProcessExecutor(
        ReviewedProcessRegistry(
            {
                stock_daily_wire.STOCK_DAILY_PROCESS_OPERATION_ID: (
                    stock_daily_wire.fetch_stock_daily_process
                )
            }
        ),
        max_parameter_bytes=1,
        max_result_bytes=stock_daily_wire.MAX_PROCESS_RESULT_JSON_BYTES,
    )
    with pytest.raises(ValueError, match="parameter cap"):
        build_stock_daily_process_source(executor)


def test_stock_daily_process_source_requires_http_and_cleanup_timeout_margin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fixed parent deadline must exceed HTTP blocking plus startup/cleanup margin."""
    from opendata.pipeline.process_executor import ReviewedProcessExecutor, ReviewedProcessRegistry

    executor = ReviewedProcessExecutor(
        ReviewedProcessRegistry(
            {
                stock_daily_wire.STOCK_DAILY_PROCESS_OPERATION_ID: (
                    stock_daily_wire.fetch_stock_daily_process
                )
            }
        ),
        max_result_bytes=stock_daily_wire.MAX_PROCESS_RESULT_JSON_BYTES,
    )
    monkeypatch.setattr(stock_daily_job, "STOCK_DAILY_PROCESS_TIMEOUT_SECONDS", 25.0)

    with pytest.raises(ValueError, match="deadline.*margin"):
        build_stock_daily_process_source(executor)


def test_stock_daily_process_handler_remains_job_fetch_for_heartbeat_checks(
    tmp_path: Path,
) -> None:
    """The process handler stays intact on the disabled job for heartbeat validation."""
    from opendata.pipeline.process_executor import ReviewedProcessExecutor, ReviewedProcessRegistry
    from opendata.pipeline.reviewed_worker import ProcessFetchHandler

    executor = ReviewedProcessExecutor(
        ReviewedProcessRegistry(
            {
                stock_daily_wire.STOCK_DAILY_PROCESS_OPERATION_ID: (
                    stock_daily_wire.fetch_stock_daily_process
                )
            }
        ),
        max_result_bytes=stock_daily_wire.MAX_PROCESS_RESULT_JSON_BYTES,
    )
    process_source = build_stock_daily_process_source(executor)
    job = build_stock_daily_job(
        _task(),
        schema_version=SCHEMA_VERSION,
        semantic_version=SEMANTIC_VERSION,
        contract_registry=DatasetContractRegistry([_contract()]),
        artifact_writer=RawResponseArtifactWriter(tmp_path / "private-process-raw"),
        process_source=process_source,
    )

    assert isinstance(process_source, ProcessFetchHandler)
    assert job.fetch is process_source
    assert process_source.heartbeat_interval_seconds == executor._checkpoint_interval
    assert job.task.enabled is False
    assert job.reviewed is False


def test_stock_daily_job_rejects_callable_that_is_not_a_process_handler(
    tmp_path: Path,
) -> None:
    """The process source path accepts only the validated bounded handler type."""

    def arbitrary_source(
        request: RunRequest,
        context: RunExecutionContext,
    ) -> StockDailyJsonResponse:
        raise AssertionError("unregistered process source must not be called")

    with pytest.raises(TypeError, match="fixed ProcessFetchHandler"):
        build_stock_daily_job(
            _task(),
            schema_version=SCHEMA_VERSION,
            semantic_version=SEMANTIC_VERSION,
            contract_registry=DatasetContractRegistry([_contract()]),
            artifact_writer=RawResponseArtifactWriter(tmp_path / "private-process-raw"),
            process_source=arbitrary_source,
        )


@pytest.mark.parametrize(
    ("custom_binding", "message"),
    [
        ("parameters_builder", "parameter builder.*fixed"),
        ("result_decoder", "result decoder.*fixed"),
        ("timeout", "timeout.*fixed"),
    ],
)
def test_stock_daily_job_rejects_unbound_process_handler_configuration(
    tmp_path: Path,
    custom_binding: str,
    message: str,
) -> None:
    """Custom IPC conversions and deadlines cannot enter the job fetch path."""
    from opendata.pipeline.process_executor import ReviewedProcessExecutor, ReviewedProcessRegistry
    from opendata.pipeline.reviewed_worker import ProcessFetchHandler

    executor = ReviewedProcessExecutor(
        ReviewedProcessRegistry(
            {
                stock_daily_wire.STOCK_DAILY_PROCESS_OPERATION_ID: (
                    stock_daily_wire.fetch_stock_daily_process
                )
            }
        ),
        max_result_bytes=stock_daily_wire.MAX_PROCESS_RESULT_JSON_BYTES,
    )
    fixed_source = build_stock_daily_process_source(executor)

    def custom_parameters(_request: RunRequest) -> dict[str, str]:
        return {"symbol": "000001", "start_date": "20260101", "end_date": "20261231"}

    def custom_decoder(value: object) -> object:
        return value

    parameters_builder = fixed_source._parameters_builder
    result_decoder = fixed_source._result_decoder
    timeout_seconds = fixed_source._timeout_seconds
    if custom_binding == "parameters_builder":
        parameters_builder = custom_parameters
    elif custom_binding == "result_decoder":
        result_decoder = custom_decoder
    else:
        timeout_seconds += 1.0
    custom_source = ProcessFetchHandler(
        executor,
        stock_daily_wire.STOCK_DAILY_PROCESS_OPERATION_ID,
        parameters_builder,
        result_decoder,
        timeout_seconds=timeout_seconds,
    )

    with pytest.raises(ValueError, match=message):
        build_stock_daily_job(
            _task(),
            schema_version=SCHEMA_VERSION,
            semantic_version=SEMANTIC_VERSION,
            contract_registry=DatasetContractRegistry([_contract()]),
            artifact_writer=RawResponseArtifactWriter(tmp_path / "private-process-raw"),
            process_source=custom_source,
        )


def test_stock_daily_job_rejects_process_handler_subclass_before_execution(
    tmp_path: Path,
) -> None:
    """A subclass cannot replace the fixed process adapter's __call__ behavior."""
    from opendata.pipeline.process_executor import ReviewedProcessExecutor, ReviewedProcessRegistry
    from opendata.pipeline.reviewed_worker import ProcessFetchHandler

    class ReplacedCallHandler(ProcessFetchHandler):
        def __call__(self, request: RunRequest, context: RunExecutionContext) -> object:
            raise AssertionError("subclass handler must not execute")

    executor = ReviewedProcessExecutor(
        ReviewedProcessRegistry(
            {
                stock_daily_wire.STOCK_DAILY_PROCESS_OPERATION_ID: (
                    stock_daily_wire.fetch_stock_daily_process
                )
            }
        ),
        max_result_bytes=stock_daily_wire.MAX_PROCESS_RESULT_JSON_BYTES,
    )
    fixed_source = build_stock_daily_process_source(executor)
    subclass_source = ReplacedCallHandler(
        executor,
        stock_daily_wire.STOCK_DAILY_PROCESS_OPERATION_ID,
        fixed_source._parameters_builder,
        fixed_source._result_decoder,
        timeout_seconds=fixed_source._timeout_seconds,
    )

    with pytest.raises(TypeError, match="fixed ProcessFetchHandler"):
        build_stock_daily_job(
            _task(),
            schema_version=SCHEMA_VERSION,
            semantic_version=SEMANTIC_VERSION,
            contract_registry=DatasetContractRegistry([_contract()]),
            artifact_writer=RawResponseArtifactWriter(tmp_path / "private-process-raw"),
            process_source=subclass_source,
        )


def test_stock_daily_process_source_rejects_executor_subclass_with_overridden_run(
    tmp_path: Path,
) -> None:
    """Only the concrete reviewed executor implementation can reach this adapter."""
    from opendata.pipeline.process_executor import (
        ReviewedProcessExecutor,
        ReviewedProcessRegistry,
    )
    from opendata.pipeline.reviewed_worker import ProcessFetchHandler

    class ReplacedRunExecutor(ReviewedProcessExecutor):
        def run(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("subclass executor must not run")

    registry = ReviewedProcessRegistry(
        {
            stock_daily_wire.STOCK_DAILY_PROCESS_OPERATION_ID: (
                stock_daily_wire.fetch_stock_daily_process
            )
        }
    )
    executor = ReplacedRunExecutor(
        registry,
        max_result_bytes=stock_daily_wire.MAX_PROCESS_RESULT_JSON_BYTES,
    )
    with pytest.raises(TypeError, match="ReviewedProcessExecutor"):
        build_stock_daily_process_source(executor)

    custom_source = ProcessFetchHandler(
        executor,
        stock_daily_wire.STOCK_DAILY_PROCESS_OPERATION_ID,
        stock_daily_job._stock_daily_process_parameters,
        stock_daily_job._decode_stock_daily_process_result,
        timeout_seconds=stock_daily_wire.STOCK_DAILY_PROCESS_TIMEOUT_SECONDS,
    )
    with pytest.raises(ValueError, match="ReviewedProcessExecutor"):
        build_stock_daily_job(
            _task(),
            schema_version=SCHEMA_VERSION,
            semantic_version=SEMANTIC_VERSION,
            contract_registry=DatasetContractRegistry([_contract()]),
            artifact_writer=RawResponseArtifactWriter(tmp_path / "private-process-raw"),
            process_source=custom_source,
        )


def test_stock_daily_process_source_rejects_instance_run_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An instance-level run replacement is rejected before any process starts."""
    from opendata.pipeline.process_executor import ReviewedProcessExecutor, ReviewedProcessRegistry

    executor = ReviewedProcessExecutor(
        ReviewedProcessRegistry(
            {
                stock_daily_wire.STOCK_DAILY_PROCESS_OPERATION_ID: (
                    stock_daily_wire.fetch_stock_daily_process
                )
            }
        ),
        max_result_bytes=stock_daily_wire.MAX_PROCESS_RESULT_JSON_BYTES,
    )

    def fake_run(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("overridden executor must not run")

    monkeypatch.setattr(executor, "run", fake_run)
    with pytest.raises(ValueError, match="run method is not the reviewed implementation"):
        build_stock_daily_process_source(executor)


@pytest.mark.parametrize(
    ("changed_config", "message"),
    [
        ("result_cap", "result cap"),
        ("poll_interval", "poll interval"),
        ("shutdown_margin", "deadline.*margin"),
        ("checkpoint", "heartbeat interval.*checkpoint config"),
    ],
)
def test_stock_daily_job_revalidates_process_executor_configuration(
    tmp_path: Path,
    changed_config: str,
    message: str,
) -> None:
    """A once-valid adapter is rejected if its resource or heartbeat config drifts."""
    from opendata.pipeline.process_executor import ReviewedProcessExecutor, ReviewedProcessRegistry

    executor = ReviewedProcessExecutor(
        ReviewedProcessRegistry(
            {
                stock_daily_wire.STOCK_DAILY_PROCESS_OPERATION_ID: (
                    stock_daily_wire.fetch_stock_daily_process
                )
            }
        ),
        max_result_bytes=stock_daily_wire.MAX_PROCESS_RESULT_JSON_BYTES,
    )
    process_source = build_stock_daily_process_source(executor)
    if changed_config == "result_cap":
        executor._max_result_bytes = stock_daily_wire.MAX_PROCESS_RESULT_JSON_BYTES - 1
    elif changed_config == "poll_interval":
        executor._poll_interval = 0.0
    elif changed_config == "shutdown_margin":
        executor._shutdown_grace = 2.5
    else:
        executor._checkpoint_interval = process_source.heartbeat_interval_seconds + 1.0

    with pytest.raises(ValueError, match=message):
        build_stock_daily_job(
            _task(),
            schema_version=SCHEMA_VERSION,
            semantic_version=SEMANTIC_VERSION,
            contract_registry=DatasetContractRegistry([_contract()]),
            artifact_writer=RawResponseArtifactWriter(tmp_path / "private-process-raw"),
            process_source=process_source,
        )


def test_stock_daily_process_decoder_to_parent_normalize_preserves_response_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Synthetic envelope reaches parent normalize without Popen or an upstream fetch."""
    from opendata.pipeline.process_executor import ReviewedProcessExecutor, ReviewedProcessRegistry

    body = _wire_body()
    encoded_body = base64.b64encode(body).decode("ascii")
    envelope = {
        "body_b64": encoded_body,
        "status_code": 200,
        "retrieved_at": "2026-10-27T08:15:00Z",
    }
    executor = ReviewedProcessExecutor(
        ReviewedProcessRegistry(
            {
                stock_daily_wire.STOCK_DAILY_PROCESS_OPERATION_ID: (
                    stock_daily_wire.fetch_stock_daily_process
                )
            }
        ),
        max_result_bytes=stock_daily_wire.MAX_PROCESS_RESULT_JSON_BYTES,
    )
    process_source = build_stock_daily_process_source(executor)
    artifact_writer = RawResponseArtifactWriter(tmp_path / "private-process-raw")
    captures = []
    original_capture = artifact_writer.capture

    def record_capture(*args: object, **kwargs: object):
        capture = original_capture(*args, **kwargs)
        captures.append(capture)
        return capture

    monkeypatch.setattr(artifact_writer, "capture", record_capture)

    job = build_stock_daily_job(
        _task(),
        schema_version=SCHEMA_VERSION,
        semantic_version=SEMANTIC_VERSION,
        contract_registry=DatasetContractRegistry([_contract()]),
        artifact_writer=artifact_writer,
        process_source=process_source,
    )
    request = _request()
    context = _context()
    parent_response = stock_daily_job._decode_stock_daily_process_result(envelope)
    assert captures == []
    normalized = job.normalize(parent_response, request, context)

    assert type(parent_response) is StockDailyJsonResponse
    assert parent_response.body == body
    assert parent_response.status_code == 200
    assert parent_response.observed_at == datetime(2026, 10, 27, 8, 15, tzinfo=UTC)
    assert len(captures) == 1
    assert captures[0].manifest.http_status == 200
    assert normalized.records[0].business_date == date(2026, 10, 26)
    assert normalized.records[0].observed_at == datetime(2026, 10, 27, 8, 15, tzinfo=UTC)
    assert normalized.records[0].business_date != normalized.records[0].observed_at.date()
    assert normalized.provenance["source_verification"] == "not_performed"
    assert normalized.provenance["source_history_limit"] == "unknown"
    assert normalized.provenance["acquisition_mode"] == "process_bounded_json"
    manifest_json = json.dumps(captures[0].manifest.model_dump(mode="json"))
    assert encoded_body not in manifest_json
    assert body.decode("utf-8") not in manifest_json
