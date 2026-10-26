"""Verify credential-bearing RunRequest payloads cannot enter the durable ledger."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, func, select

from opendata.pipeline.run_identity import RunRequest, ScheduleMetadata, TaskDefinition
from opendata.pipeline.run_ledger import RUN_LEDGER_METADATA, RUN_LEDGER_TABLE, RunLedger
from opendata.pipeline.run_service import ReviewedJob, ReviewedJobRegistry, RunService


@pytest.fixture
def run_service(tmp_path):
    """Create a reviewed service backed by a disposable, owned SQLite ledger."""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'credential-runs.db'}")
    RUN_LEDGER_METADATA.create_all(engine)
    ledger = RunLedger(engine)
    task = TaskDefinition(
        job_id="credential-boundary",
        version="v1",
        dataset_id="market.daily",
        provider="openbb",
        model="equity_historical",
        schedule=ScheduleMetadata(kind="cron", expression="0 17 * * 1-5"),
        enabled=True,
    )
    job = ReviewedJob(
        task=task,
        fetch=lambda _request, _context: None,
        normalize=lambda _raw, _request, _context: None,
        publish=lambda _connection, _batch: None,
        reviewed=True,
    )
    service = RunService(ledger, ReviewedJobRegistry([job]))
    try:
        yield service, ledger, engine
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "query",
    [
        {"database_url": "mysql://readonly:credential-canary@db.invalid/warehouse"},
        {
            "filters": [
                {"Connection_String": "mysql://readonly:credential-canary@db.invalid/warehouse"}
            ]
        },
        {"source": "mysql://readonly:credential-canary@db.invalid/warehouse"},
        {"endpoint": "jdbc:postgresql://readonly:credential-canary@db.invalid/warehouse"},
        {"endpoint": "jdbc:oracle:thin:readonly/credential-canary@db.invalid:1521/prod"},
        {
            "endpoint": "Database settings: jdbc:mysql://readonly:credential-canary@db.invalid/warehouse"
        },
        {"endpoint": "Driver=ODBC Driver 18;Server=db.invalid;UID=readonly;PWD=credential-canary;"},
        {"endpoint": "Driver=ODBC Driver 18;Server=db.invalid;UID=readonly;"},
        {"endpoint": "PWD=credential-canary"},
        {"endpoint": "password=credential-canary"},
        {"endpoint": "passwd=credential-canary"},
        {"endpoint": "https://data.example.test/prices?password=credential-canary"},
        {"endpoint": "https://data.example.test/prices?token=credential-canary"},
        {"endpoint": "https://data.example.test/prices?api_key=credential-canary"},
        {"endpoint": "https://readonly:credential-canary@[::1"},
        {"endpoint": "https://[::1/?api_key=credential-canary"},
        {"sources": [{"endpoint": "jdbc:postgresql://readonly:credential-canary@db.invalid/data"}]},
    ],
)
def test_service_rejects_credentials_before_persisting_or_echoing_them(run_service, query):
    """Revalidate constructed payloads before identity serialization and ledger writes."""
    service, _ledger, engine = run_service
    request = RunRequest.model_construct(
        run_type="manual",
        idempotency_key="manual-credential-canary",
        query=query,
        partition={},
    )

    with pytest.raises(ValidationError) as caught:
        service.submit("credential-boundary", "v1", request)

    assert "credential-canary" not in str(caught.value)
    with engine.connect() as connection:
        row_count = connection.scalar(select(func.count()).select_from(RUN_LEDGER_TABLE))
    assert row_count == 0


def test_service_does_not_echo_sensitive_query_keys(run_service):
    """Reject crafted sensitive keys without leaking them or writing a row."""
    service, _ledger, engine = run_service
    canary = "leaky-canary-918273"
    request = RunRequest.model_construct(
        run_type="manual",
        idempotency_key="manual-sensitive-key",
        query={f"api_key={canary}": "safe business value"},
        partition={},
    )

    with pytest.raises(ValidationError) as caught:
        service.submit("credential-boundary", "v1", request)

    assert canary not in str(caught.value)
    with engine.connect() as connection:
        row_count = connection.scalar(select(func.count()).select_from(RUN_LEDGER_TABLE))
    assert row_count == 0
