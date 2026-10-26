"""Contract-validated publication through a real SQLite fenced run ledger."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest
from opendata.data.dataset_contract_registry import (
    DatasetContractNotFoundError,
    DatasetContractRegistry,
)
from opendata.data.platform_contracts import (
    DatasetContract,
    FieldContract,
    SourceIdentity,
    canonical_sha256,
)
from opendata.pipeline.publication_store import (
    PUBLICATION_HEADS,
    PUBLICATION_METADATA,
    PUBLICATIONS,
    PUBLISHED_FACT_VERSIONS,
)
from sqlalchemy import create_engine, select

from opendata.pipeline.contract_publication import (
    ContractPublicationError,
    ValidatedPublicationWriter,
    _contract_business_key,
    _contract_time_values,
    _parse_aware_datetime,
    _provenance_summary,
    _quality_summary,
    _require_declared_unit,
    _same_instant,
    _source_available_at,
    _unknown_label,
    _validate_field_value,
    _validate_record,
)
from opendata.pipeline.run_identity import RunRequest, ScheduleMetadata, TaskDefinition
from opendata.pipeline.run_ledger import RUN_LEDGER_METADATA, RUN_LEDGER_TABLE, RunLedger
from opendata.pipeline.run_service import (
    NormalizedBatch,
    NormalizedRecord,
    ReviewedJob,
    ReviewedJobRegistry,
    RunService,
)

UTC = timezone.utc
NOW = datetime(2026, 10, 26, 2, 0, tzinfo=UTC)
TTL = timedelta(minutes=2)
KEY = json.dumps(["SSE:600000", "2026-10-26"], ensure_ascii=False, separators=(",", ":"))


def equity_contract(
    *,
    schema_version: str = "1.0.0",
    unit: str = "CNY/share",
    adjustment_basis: str = "raw",
) -> DatasetContract:
    """Create a strict daily-equity contract with two field-specific units."""
    return DatasetContract(
        dataset_id="cn_equity_daily",
        schema_version=schema_version,
        semantic_version="equity-bars-v1",
        asset_class="equity",
        frequency="1d",
        record_kind="time_series",
        fields=(
            FieldContract(name="instrument_id", data_type="string"),
            FieldContract(name="trade_date", data_type="date"),
            FieldContract(name="close", data_type="number"),
            FieldContract(name="volume", data_type="integer"),
        ),
        business_key=("instrument_id", "trade_date"),
        ordering=("trade_date", "instrument_id"),
        filter_fields=("instrument_id",),
        time_field="trade_date",
        temporal_semantics="business_date",
        source=SourceIdentity(source_id="cn_equity_daily", source="eastmoney"),
        field_units={"close": unit, "volume": "share"},
        currency="CNY",
        adjustment_basis=adjustment_basis,
        field_adjustments={"close": adjustment_basis, "volume": "not_applicable"},
        calendar_id="SSE",
        timezone="Asia/Shanghai",
        pit_policy="unsupported",
    )


def make_task(
    *, provider: str = "eastmoney", dataset_id: str = "cn_equity_daily"
) -> TaskDefinition:
    return TaskDefinition(
        job_id="stock-daily",
        version="v1",
        dataset_id=dataset_id,
        provider=provider,
        model="equity-daily-v1",
        schedule=ScheduleMetadata(kind="cron", expression="0 16 * * 1-5"),
        enabled=True,
    )


def make_batch(
    *,
    key: str = KEY,
    payload: dict[str, object] | None = None,
    unit: str = "per-field",
    adjustment_basis: str = "raw",
    quality: dict[str, object] | None = None,
    provenance: dict[str, object] | None = None,
) -> NormalizedBatch:
    return NormalizedBatch(
        records=(
            NormalizedRecord(
                business_key=key,
                payload=payload
                if payload is not None
                else {
                    "instrument_id": "SSE:600000",
                    "trade_date": "2026-10-26",
                    "close": 12.34,
                    "volume": 1200,
                },
                business_date=date(2026, 10, 26),
            ),
        ),
        schema_version="1.0.0",
        semantic_version="equity-bars-v1",
        unit=unit,
        adjustment_basis=adjustment_basis,
        quality=quality or {},
        provenance=provenance or {"adapter": "equity-daily-v1"},
    )


def setup_engine():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    RUN_LEDGER_METADATA.create_all(engine)
    PUBLICATION_METADATA.create_all(engine)
    ledger = RunLedger(engine)
    return engine, ledger


def make_service(
    contract: DatasetContract,
    *,
    task: TaskDefinition | None = None,
    batch: NormalizedBatch | None = None,
):
    selected_task = task or make_task()
    selected_batch = batch or make_batch()
    writer = ValidatedPublicationWriter(DatasetContractRegistry([contract]))
    job = ReviewedJob(
        task=selected_task,
        reviewed=True,
        fetch=lambda _request, _context: {"rows": 1},
        normalize=lambda _raw, _request, _context: selected_batch,
        publish=writer,
    )
    engine, ledger = setup_engine()
    service = RunService(
        ledger,
        ReviewedJobRegistry([job]),
        clock=lambda: datetime.now(UTC),
    )
    return engine, ledger, service


def test_real_ledger_and_store_publish_contract_metadata_atomically_once() -> None:
    contract = equity_contract()
    engine, ledger, service = make_service(contract)
    try:
        request = RunRequest(run_type="manual", idempotency_key="request-1")
        first = service.submit("stock-daily", "v1", request)
        replay = service.submit("stock-daily", "v1", request)
        outcome = service.run_once("worker-a", TTL)

        assert replay.run_key == first.run_key
        assert outcome is not None and outcome.record is not None
        assert outcome.record.status == "succeeded"
        assert outcome.publication is not None
        assert outcome.publication.dataset_id == contract.dataset_id
        assert outcome.publication.record_count == 1
        assert service.run_once("worker-a", TTL) is None
        with engine.connect() as connection:
            publications = connection.execute(select(PUBLICATIONS)).mappings().all()
            facts = connection.execute(select(PUBLISHED_FACT_VERSIONS)).mappings().all()
            run = (
                connection.execute(
                    select(RUN_LEDGER_TABLE).where(RUN_LEDGER_TABLE.c.run_key == first.run_key)
                )
                .mappings()
                .one()
            )

        assert len(publications) == 1
        assert len(facts) == 1
        assert publications[0]["source_id"] == "cn_equity_daily"
        assert publications[0]["unit"] == "per-field"
        assert publications[0]["adjustment_basis"] == "raw"
        assert json.loads(publications[0]["quality_json"]) == {
            "status": "unknown",
            "report_id": None,
            "issue_count": 0,
        }
        assert json.loads(publications[0]["provenance_json"])[
            "contract_sha256"
        ] == canonical_sha256(contract)
        assert facts[0]["business_key"] == KEY
        assert facts[0]["business_date"] == date(2026, 10, 26)
        assert run["status"] == "succeeded"
    finally:
        engine.dispose()


def test_event_and_source_availability_times_keep_distinct_meaning() -> None:
    event_contract = DatasetContract(
        dataset_id="company_filings",
        schema_version="1.0.0",
        semantic_version="filing-events-v1",
        asset_class="financial",
        frequency="irregular",
        record_kind="event",
        fields=(
            FieldContract(name="filing_id", data_type="string"),
            FieldContract(name="event_time", data_type="datetime"),
            FieldContract(name="source_available_at", data_type="datetime"),
            FieldContract(name="amount", data_type="number"),
        ),
        business_key=("filing_id", "event_time"),
        ordering=("event_time", "filing_id"),
        time_field="event_time",
        temporal_semantics="event_time",
        source_available_at_field="source_available_at",
        source=SourceIdentity(source_id="company_filings", source="sec"),
        field_units={"amount": "USD"},
        adjustment_basis="not_applicable",
        field_adjustments={"amount": "not_applicable"},
        timezone="UTC",
        pit_policy="supported",
    )
    source_time = NOW + timedelta(minutes=1)
    available_time = NOW + timedelta(minutes=2)
    event_key = json.dumps(
        ["filing-1", NOW.isoformat(timespec="microseconds").replace("+00:00", "Z")],
        separators=(",", ":"),
    )
    event_batch = NormalizedBatch(
        records=(
            NormalizedRecord(
                business_key=event_key,
                payload={
                    "filing_id": "filing-1",
                    "event_time": NOW.isoformat().replace("+00:00", "Z"),
                    "source_available_at": source_time.isoformat().replace("+00:00", "Z"),
                    "amount": 125.5,
                },
                event_at=NOW,
                source_published_at=source_time,
                available_at=available_time,
            ),
        ),
        schema_version="1.0.0",
        semantic_version="filing-events-v1",
        unit="per-field",
        adjustment_basis="not_applicable",
        quality={},
        provenance={"adapter": "sec-filings-v1"},
    )
    event_task = make_task(provider="sec", dataset_id="company_filings")
    engine, _, service = make_service(event_contract, task=event_task, batch=event_batch)
    try:
        service.submit(
            "stock-daily", "v1", RunRequest(run_type="manual", idempotency_key="filing-1")
        )
        outcome = service.run_once("worker-a", TTL)
        assert outcome is not None and outcome.record is not None
        assert outcome.record.status == "succeeded"
        with engine.connect() as connection:
            fact = connection.execute(select(PUBLISHED_FACT_VERSIONS)).mappings().one()
        assert fact["business_date"] is None
        assert fact["event_at"] == NOW.isoformat(timespec="microseconds").replace("+00:00", "Z")
        assert fact["source_published_at"] == source_time.isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z")
        assert fact["available_at"] == available_time.isoformat(timespec="microseconds").replace(
            "+00:00", "Z"
        )
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("contract", "task", "batch"),
    [
        (equity_contract(), make_task(provider="wrong-source"), make_batch()),
        (equity_contract(unit="unknown"), make_task(), make_batch()),
        (equity_contract(), make_task(), make_batch(unit="unknown")),
        (equity_contract(), make_task(), make_batch(adjustment_basis="unknown")),
        (equity_contract(), make_task(), make_batch(key="wrong-key")),
        (
            equity_contract(),
            make_task(),
            make_batch(
                payload={
                    "instrument_id": "SSE:600000",
                    "trade_date": "2026-10-25",
                    "close": 12.34,
                    "volume": 1200,
                }
            ),
        ),
        (
            equity_contract(),
            make_task(),
            make_batch(provenance={"contract_sha256": "caller-value"}),
        ),
    ],
)
def test_contract_errors_fail_the_run_without_creating_publications(
    contract: DatasetContract, task: TaskDefinition, batch: NormalizedBatch
) -> None:
    engine, ledger, service = make_service(contract, task=task, batch=batch)
    try:
        service.submit("stock-daily", "v1", RunRequest(run_type="manual", idempotency_key="bad"))
        outcome = service.run_once("worker-a", TTL)
        assert outcome is not None and outcome.record is not None
        assert outcome.record.status == "failed"
        assert outcome.publication is None
        with engine.connect() as connection:
            publications = connection.execute(select(PUBLICATIONS)).all()
        assert publications == []
    finally:
        engine.dispose()


def test_contract_version_must_be_registered_exactly() -> None:
    contract = equity_contract(schema_version="2.0.0")
    engine, _, service = make_service(contract, batch=make_batch())
    try:
        service.submit(
            "stock-daily", "v1", RunRequest(run_type="manual", idempotency_key="wrong-schema")
        )
        outcome = service.run_once("worker-a", TTL)
        assert outcome is not None and outcome.record is not None
        assert outcome.record.status == "failed"
        with engine.connect() as connection:
            assert connection.execute(select(PUBLICATIONS)).all() == []
    finally:
        engine.dispose()


def test_quality_status_needs_evidence_and_writers_do_not_infer_passed() -> None:
    engine, _ledger, service = make_service(
        equity_contract(),
        batch=make_batch(quality={"status": "passed", "report_id": None, "issue_count": 0}),
    )
    try:
        service.submit(
            "stock-daily", "v1", RunRequest(run_type="manual", idempotency_key="bad-quality")
        )
        outcome = service.run_once("worker-a", TTL)
        assert outcome is not None and outcome.record is not None
        assert outcome.record.status == "failed"
        with engine.connect() as connection:
            assert connection.execute(select(PUBLICATIONS)).all() == []
    finally:
        engine.dispose()


def test_rejected_quality_fails_run_without_publication_or_watermark_promotion() -> None:
    rejected_quality = {"status": "rejected", "report_id": "qa-rejected", "issue_count": 1}
    assert _quality_summary(rejected_quality) == rejected_quality
    batch = replace(
        make_batch(quality=rejected_quality),
        watermark={"observed_through": "2026-10-26"},
    )
    engine, ledger, service = make_service(equity_contract(), batch=batch)
    try:
        submitted = service.submit(
            "stock-daily",
            "v1",
            RunRequest(run_type="manual", idempotency_key="rejected-quality"),
        )
        outcome = service.run_once("worker-a", TTL)

        assert outcome is not None and outcome.record is not None
        assert outcome.record.run_key == submitted.run_key
        assert outcome.record.status == "failed"
        assert outcome.publication is None
        stored_run = ledger.get(submitted.run_key)
        assert stored_run is not None and stored_run.status == "failed"
        with engine.connect() as connection:
            publications = connection.execute(select(PUBLICATIONS)).all()
            facts = connection.execute(select(PUBLISHED_FACT_VERSIONS)).all()
            heads = connection.execute(select(PUBLICATION_HEADS)).all()
            run = (
                connection.execute(
                    select(RUN_LEDGER_TABLE).where(RUN_LEDGER_TABLE.c.run_key == submitted.run_key)
                )
                .mappings()
                .one()
            )

        assert publications == []
        assert facts == []
        assert heads == []
        assert run["status"] == "failed"
    finally:
        engine.dispose()


def test_registry_requires_an_explicit_contract_registry() -> None:
    with pytest.raises(TypeError, match="DatasetContractRegistry"):
        ValidatedPublicationWriter(object())  # type: ignore[arg-type]
    with pytest.raises(DatasetContractNotFoundError):
        DatasetContractRegistry([equity_contract()]).resolve("missing", "1.0.0", "equity-bars-v1")


@pytest.mark.parametrize(
    ("data_type", "value"),
    [
        ("string", "value"),
        ("integer", 3),
        ("number", 3.25),
        ("boolean", True),
        ("date", "2026-10-26"),
        ("datetime", "2026-10-26T02:00:00Z"),
        ("object", {"k": 1}),
        ("array", [1, "two"]),
    ],
)
def test_contract_field_types_accept_only_their_declared_wire_values(
    data_type: str, value: object
) -> None:
    field = FieldContract(name="value", data_type=data_type)  # type: ignore[arg-type]
    assert _validate_field_value("value", value, field) == value


@pytest.mark.parametrize(
    ("data_type", "value"),
    [
        ("string", 1),
        ("integer", True),
        ("number", True),
        ("number", float("inf")),
        ("boolean", 1),
        ("date", "20261026"),
        ("date", "not-a-date"),
        ("date", 20261026),
        ("datetime", 2026102602),
        ("datetime", "2026-10-26T02:00:00"),
        ("datetime", "bad-time"),
        ("object", []),
        ("array", (1, 2)),
    ],
)
def test_contract_field_type_mismatches_are_rejected(data_type: str, value: object) -> None:
    field = FieldContract(name="value", data_type=data_type)  # type: ignore[arg-type]
    with pytest.raises(ContractPublicationError):
        _validate_field_value("value", value, field)


def test_nullable_contract_fields_and_composite_keys_are_explicit() -> None:
    optional = FieldContract(name="optional", data_type="string", nullable=True)
    assert _validate_field_value("optional", None, optional) is None
    required = FieldContract(name="required", data_type="string", nullable=False)
    with pytest.raises(ContractPublicationError, match="required field"):
        _validate_field_value("required", None, required)

    contract = equity_contract()
    with pytest.raises(ContractPublicationError, match="non-empty"):
        _contract_business_key(
            contract,
            {"instrument_id": "", "trade_date": "2026-10-26"},
        )
    assert (
        _contract_business_key(
            contract,
            {"instrument_id": "SSE:600000", "trade_date": "2026-10-26"},
        )
        == KEY
    )
    unsupported = type("Field", (), {"data_type": "unsupported", "nullable": False})()
    with pytest.raises(ContractPublicationError, match="unsupported contract type"):
        _validate_field_value("value", "x", unsupported)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "quality",
    [
        {"status": "passed"},
        {"status": "false", "report_id": "qa-1", "issue_count": 0},
        {"status": [], "report_id": "qa-1", "issue_count": 0},
        {"status": "passed", "report_id": "", "issue_count": 0},
        {"status": "passed", "report_id": "qa-1", "issue_count": True},
        {"status": "passed", "report_id": None, "issue_count": 0},
        {"status": "passed", "report_id": "qa-1", "issue_count": 1},
        {"status": "rejected", "report_id": "qa-1", "issue_count": 0},
    ],
)
def test_quality_shape_rejects_missing_or_contradictory_evidence(
    quality: dict[str, object],
) -> None:
    with pytest.raises(ContractPublicationError):
        _quality_summary(quality)


def test_quality_status_and_contract_provenance_are_derived_explicitly() -> None:
    assert _quality_summary({}) == {"status": "unknown", "report_id": None, "issue_count": 0}
    assert _quality_summary({"status": "passed", "report_id": "qa-20261026", "issue_count": 0}) == {
        "status": "passed",
        "report_id": "qa-20261026",
        "issue_count": 0,
    }
    contract = equity_contract()
    summary = _provenance_summary({"adapter": "v1"}, contract, "a" * 64)
    assert summary == {
        "adapter": "v1",
        "source": "eastmoney",
        "source_id": "cn_equity_daily",
        "contract_sha256": "a" * 64,
    }
    for provenance in (
        {"contract_sha256": "caller"},
        {"source": "wrong"},
        {"source_id": "wrong"},
        {"provider": "wrong"},
    ):
        with pytest.raises(ContractPublicationError):
            _provenance_summary(provenance, contract, "a" * 64)


def test_timestamp_and_observation_semantics_do_not_conflate_date_kinds() -> None:
    event_contract = DatasetContract(
        dataset_id="event-observations",
        schema_version="1.0.0",
        semantic_version="event-v1",
        asset_class="other",
        frequency="snapshot",
        record_kind="snapshot",
        fields=(
            FieldContract(name="key", data_type="string"),
            FieldContract(name="observed", data_type="datetime"),
        ),
        business_key=("key", "observed"),
        ordering=("observed", "key"),
        time_field="observed",
        temporal_semantics="observation_time",
        source=SourceIdentity(source_id="event-observations", source="feed"),
        adjustment_basis="not_applicable",
        timezone="UTC",
        pit_policy="required",
    )
    row = NormalizedRecord(
        business_key='["x","2026-10-26T02:00:00.000000Z"]',
        payload={"key": "x", "observed": "2026-10-26T02:00:00Z"},
        observed_at=NOW,
    )
    assert _contract_time_values(row.payload, row, event_contract) == (None, None, NOW)
    with pytest.raises(ContractPublicationError, match="snapshot time"):
        _contract_time_values(
            row.payload, replace(row, business_date=date(2026, 10, 26)), event_contract
        )
    with pytest.raises(ContractPublicationError, match="snapshot time"):
        _contract_time_values(row.payload, replace(row, event_at=NOW), event_contract)

    assert _parse_aware_datetime("2026-10-26T04:00:00+02:00", "time") == NOW
    with pytest.raises(ContractPublicationError, match="include a timezone"):
        _parse_aware_datetime("2026-10-26T02:00:00", "time")
    with pytest.raises(ContractPublicationError, match="invalid timestamp"):
        _parse_aware_datetime("not-a-time", "time")
    assert _same_instant(NOW, NOW)
    assert not _same_instant(None, NOW)
    assert not _same_instant(datetime(2026, 10, 26, 2), NOW)


@pytest.mark.parametrize("label", ["", "unknown", "Unknown", "unspecified", "n/a", "na", "none"])
def test_unknown_unit_and_semantic_labels_fail_closed(label: str) -> None:
    assert _unknown_label(label)
    with pytest.raises(ContractPublicationError, match="verified unit"):
        _require_declared_unit(label, "close")


def test_adjustment_summary_must_match_contract_and_cover_numeric_fields() -> None:
    writer = ValidatedPublicationWriter(DatasetContractRegistry([equity_contract()]))
    writer._validate_batch_semantics(make_batch(), equity_contract())
    with pytest.raises(ContractPublicationError, match="adjustment basis"):
        writer._validate_batch_semantics(
            make_batch(adjustment_basis="split_adjusted"), equity_contract()
        )
    with pytest.raises(ContractPublicationError, match="per-field"):
        writer._validate_batch_semantics(make_batch(unit="unknown"), equity_contract())

    contract = equity_contract(unit="unknown")
    with pytest.raises(ContractPublicationError, match="verified unit"):
        writer._validate_batch_semantics(make_batch(), contract)

    contract = equity_contract().model_copy(
        update={"field_adjustments": {"close": "unknown", "volume": "not_applicable"}}
    )
    with pytest.raises(ContractPublicationError, match="unknown adjustment"):
        writer._validate_batch_semantics(make_batch(), contract)

    unknown_adjustment = equity_contract(adjustment_basis="unknown")
    with pytest.raises(ContractPublicationError, match="adjustment basis is unknown"):
        writer._validate_batch_semantics(make_batch(adjustment_basis="unknown"), unknown_adjustment)

    missing_time = equity_contract().model_copy(update={"time_field": None})
    with pytest.raises(ContractPublicationError, match="no time field"):
        writer._validate_batch_semantics(make_batch(), missing_time)

    wrong_date_type = equity_contract().model_copy(
        update={
            "fields": (
                FieldContract(name="instrument_id", data_type="string"),
                FieldContract(name="trade_date", data_type="string"),
                FieldContract(name="close", data_type="number"),
                FieldContract(name="volume", data_type="integer"),
            )
        }
    )
    with pytest.raises(ContractPublicationError, match="time field is not a date"):
        writer._validate_batch_semantics(make_batch(), wrong_date_type)

    wrong_timestamp_type = equity_contract().model_copy(
        update={"temporal_semantics": "event_time", "time_field": "trade_date"}
    )
    with pytest.raises(ContractPublicationError, match="not a timestamp"):
        writer._validate_batch_semantics(make_batch(), wrong_timestamp_type)

    non_temporal = equity_contract().model_copy(
        update={"temporal_semantics": "none", "time_field": None}
    )
    writer._validate_batch_semantics(make_batch(), non_temporal)

    with pytest.raises(ContractPublicationError, match="schema version"):
        writer._validate_batch_semantics(
            replace(make_batch(), schema_version="wrong"), equity_contract()
        )
    with pytest.raises(ContractPublicationError, match="semantic version"):
        writer._validate_batch_semantics(
            replace(make_batch(), semantic_version="wrong"), equity_contract()
        )


def test_source_availability_must_have_a_declared_matching_field() -> None:
    contract = equity_contract()
    row = NormalizedRecord(
        KEY,
        {
            "instrument_id": "SSE:600000",
            "trade_date": "2026-10-26",
            "close": 12.34,
            "volume": 1200,
        },
        business_date=date(2026, 10, 26),
        source_published_at=NOW,
    )
    with pytest.raises(ContractPublicationError, match="no declared contract field"):
        _source_available_at(row.payload, row, contract)

    source_field = FieldContract(name="source_available_at", data_type="datetime", nullable=True)
    contract_with_source_time = contract.model_copy(
        update={
            "fields": (*contract.fields, source_field),
            "source_available_at_field": "source_available_at",
        }
    )
    null_payload = {**row.payload, "source_available_at": None}
    null_row = replace(row, payload=null_payload, source_published_at=None)
    assert _source_available_at(null_payload, null_row, contract_with_source_time) is None
    with pytest.raises(ContractPublicationError, match="contradicts null"):
        _source_available_at(
            null_payload,
            replace(null_row, source_published_at=NOW),
            contract_with_source_time,
        )
    dated_payload = {**row.payload, "source_available_at": NOW.isoformat().replace("+00:00", "Z")}
    with pytest.raises(ContractPublicationError, match="does not match"):
        _source_available_at(dated_payload, null_row, contract_with_source_time)


def test_record_adapter_rejects_schema_key_and_date_disagreement() -> None:
    contract = equity_contract()
    with pytest.raises(ContractPublicationError, match="invalid type"):
        _validate_record(object(), contract)  # type: ignore[arg-type]
    valid_payload = {
        "instrument_id": "SSE:600000",
        "trade_date": "2026-10-26",
        "close": 12.34,
        "volume": 1200,
    }
    with pytest.raises(ContractPublicationError, match="payload must be an object"):
        _validate_record(NormalizedRecord(KEY, None), contract)  # type: ignore[arg-type]
    with pytest.raises(ContractPublicationError, match="exactly match"):
        _validate_record(NormalizedRecord(KEY, {"extra": 1}), contract)
    with pytest.raises(ContractPublicationError, match="business date"):
        _validate_record(
            NormalizedRecord(
                json.dumps(["SSE:600000", "2026-10-25"], separators=(",", ":")),
                {**valid_payload, "trade_date": "2026-10-25"},
                business_date=date(2026, 10, 26),
            ),
            contract,
        )
    with pytest.raises(ContractPublicationError, match="event time"):
        _validate_record(
            NormalizedRecord(KEY, valid_payload, business_date=date(2026, 10, 26), event_at=NOW),
            contract,
        )


def test_malformed_registry_responses_are_not_trusted() -> None:
    contract = equity_contract()

    class OverriddenRegistry(DatasetContractRegistry):
        def __init__(self, result: object) -> None:
            super().__init__([contract])
            object.__setattr__(self, "result", result)

        def resolve(self, dataset_id: str, schema_version: str, semantic_version: str):
            del dataset_id, schema_version, semantic_version
            return self.result

    with pytest.raises(ContractPublicationError, match="invalid dataset contract"):
        ValidatedPublicationWriter(OverriddenRegistry(object()))._resolve_contract(
            make_task(), make_batch()
        )
    with pytest.raises(ContractPublicationError, match="identity does not match"):
        ValidatedPublicationWriter(
            OverriddenRegistry(equity_contract(schema_version="2.0.0"))
        )._resolve_contract(make_task(), make_batch())

    unknown_version = equity_contract()
    object.__setattr__(unknown_version, "schema_version", "unknown")
    unknown_batch = replace(make_batch(), schema_version="unknown")
    with pytest.raises(ContractPublicationError, match="version identity is unknown"):
        ValidatedPublicationWriter(OverriddenRegistry(unknown_version))._resolve_contract(
            make_task(), unknown_batch
        )

    unknown_source = equity_contract()
    object.__setattr__(unknown_source.source, "source", "unknown")
    with pytest.raises(ContractPublicationError, match="source identity is unknown"):
        ValidatedPublicationWriter(OverriddenRegistry(unknown_source))._resolve_contract(
            make_task(), make_batch()
        )


def test_temporal_rows_keep_business_event_and_observation_time_separate() -> None:
    equity = equity_contract()
    payload = {"instrument_id": "SSE:600000", "trade_date": "2026-10-26"}
    with pytest.raises(ContractPublicationError, match="no time field"):
        _contract_time_values(
            payload, NormalizedRecord(KEY, payload), equity.model_copy(update={"time_field": None})
        )

    event_contract = equity.model_copy(
        update={"temporal_semantics": "event_time", "time_field": "trade_date"}
    )
    event_payload = {**payload, "trade_date": "2026-10-26T02:00:00Z"}
    with pytest.raises(ContractPublicationError, match="event time"):
        _contract_time_values(event_payload, NormalizedRecord(KEY, event_payload), event_contract)
    with pytest.raises(ContractPublicationError, match="no time field"):
        _contract_time_values(
            event_payload,
            NormalizedRecord(KEY, event_payload),
            event_contract.model_copy(update={"time_field": None}),
        )
    with pytest.raises(ContractPublicationError, match="business date"):
        _contract_time_values(
            event_payload,
            NormalizedRecord(KEY, event_payload, event_at=NOW, business_date=date(2026, 10, 26)),
            event_contract,
        )

    missing_observation_field = equity.model_copy(
        update={"temporal_semantics": "observation_time", "time_field": None}
    )
    with pytest.raises(ContractPublicationError, match="no time field"):
        _contract_time_values(payload, NormalizedRecord(KEY, payload), missing_observation_field)
    observation_contract = equity.model_copy(
        update={"temporal_semantics": "observation_time", "time_field": "trade_date"}
    )
    observation_payload = {**payload, "trade_date": "2026-10-26T02:00:00Z"}
    with pytest.raises(ContractPublicationError, match="observation time"):
        _contract_time_values(
            observation_payload,
            NormalizedRecord(KEY, observation_payload, observed_at=NOW + timedelta(seconds=1)),
            observation_contract,
        )

    non_temporal = equity.model_copy(update={"temporal_semantics": "none", "time_field": None})
    with pytest.raises(ContractPublicationError, match="non-temporal"):
        _contract_time_values(payload, NormalizedRecord(KEY, payload, event_at=NOW), non_temporal)
    assert _contract_time_values(payload, NormalizedRecord(KEY, payload), non_temporal) == (
        None,
        None,
        None,
    )
