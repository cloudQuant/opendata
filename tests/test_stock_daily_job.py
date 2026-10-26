"""Behavioral tests for the offline-reviewed stock daily pipeline adapter."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select

import opendata.pipeline.stock_daily_job as stock_daily_job
from opendata.data.dataset_contract_registry import DatasetContractRegistry
from opendata.data.platform_contracts import DatasetContract, FieldContract, SourceIdentity
from opendata.data.providers.akshare._source import SOURCE
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
    root.mkdir()
    regular = root / "regular.bin"
    regular.write_bytes(b"ok")
    assert stock_daily_job._read_private_managed_file(root, ("regular.bin",), maximum=2) == b"ok"
    with pytest.raises(ValueError, match="absolute"):
        stock_daily_job._read_private_managed_file(Path("relative"), ("x",), maximum=1)
    with pytest.raises(ValueError, match="unsafe component"):
        stock_daily_job._read_private_managed_file(root, ("..", "x"), maximum=1)
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


def test_capture_reader_detects_in_place_file_growth(tmp_path: Path, monkeypatch) -> None:
    """The file descriptor size is rechecked after the bounded read completes."""
    root = tmp_path / "managed"
    root.mkdir()
    content = root / "content.bin"
    content.write_bytes(b"before")
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
    root.mkdir()
    content = root / "content.bin"
    content.write_bytes(b"")
    real_read = os.read

    def append_before_read(descriptor: int, count: int) -> bytes:
        with content.open("ab") as stream:
            stream.write(b"abc")
        monkeypatch.setattr(stock_daily_job.os, "read", real_read)
        return real_read(descriptor, count)

    monkeypatch.setattr(stock_daily_job.os, "read", append_before_read)
    with pytest.raises(ValueError, match="changed or exceeded"):
        stock_daily_job._read_private_managed_file(root, ("content.bin",), maximum=2)
