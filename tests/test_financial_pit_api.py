"""HTTP route proof for the financial as_of versioned read path."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import (
    BigInteger,
    Column,
    Date,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
)
from sqlalchemy.pool import StaticPool

from opendata.api.data_query import get_warehouse_engine
from opendata.data.domains import dwd_table
from opendata.data.providers.catalog import register_provider
from opendata.data.registry import ProviderRegistry
from opendata.main import app
from opendata.pipeline.ddl import DWD_TRACE_COLUMNS, contract_columns
from opendata.pipeline.financial_pit import FinancialCoverageInput, FinancialVersionInput
from opendata.services.financial_pit import (
    declare_financial_pit_coverage,
    ingest_financial_versions,
)

pytestmark = pytest.mark.integration


def _upgrade_pit_schema(engine) -> None:
    import importlib.util
    from pathlib import Path

    path = Path(__file__).parents[1] / "alembic_data/versions/20261012-0008_financial_pit.py"
    spec = importlib.util.spec_from_file_location("financial_pit_api_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
        module.upgrade()


def _statement(*, value: float, revision: int, announce_date: date = date(2024, 4, 1)):
    return {
        "symbol": "600000",
        "statement_type": "income",
        "report_period": date(2024, 3, 31),
        "announce_date": announce_date,
        "item": "net_profit",
        "value": value,
        "revision": revision,
    }


def _financial_table(metadata: MetaData) -> Table:
    primary_key = {"symbol", "statement_type", "report_period", "item"}
    type_map = {
        "varchar": String,
        "char": String,
        "double": Float,
        "bigint": BigInteger,
        "date": Date,
        "datetime": DateTime,
        "tinyint": Integer,
    }
    columns = []
    for column in [*contract_columns("financial_statement"), *DWD_TRACE_COLUMNS]:
        sql_type = column.sql_type.split("(", 1)[0]
        columns.append(
            Column(
                column.name,
                type_map[sql_type],
                primary_key=column.name in primary_key,
                nullable=column.nullable,
            )
        )
    return Table(dwd_table("financial_statement"), metadata, *columns)


@pytest.fixture(autouse=True)
def _registry_ready(monkeypatch):
    """Register the actual THS capability without loading unrelated providers."""
    registry = ProviderRegistry()
    register_provider("ths", registry)
    monkeypatch.setattr("opendata.api.data_query.get_registry", lambda: registry)
    monkeypatch.setattr("opendata.data.registry.get_registry", lambda: registry)


@pytest.fixture
def pit_warehouse(monkeypatch):
    metadata = MetaData()
    financial = _financial_table(metadata)
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    metadata.create_all(engine)
    _upgrade_pit_schema(engine)
    with engine.begin() as connection:
        connection.execute(
            financial.insert(),
            {
                **_statement(value=999.0, revision=2, announce_date=date(2024, 4, 3)),
                "source": "ths",
                "_merged_at": datetime(2026, 10, 12, 1),
                "_diff_flag": 0,
                "_as_of": date(2026, 10, 12),
            },
        )
    app.dependency_overrides[get_warehouse_engine] = lambda: engine
    record = FinancialVersionInput(
        domain="financial_statement",
        source="ths",
        market="CN",
        payload=_statement(value=10.0, revision=1),
        source_revision="source-r1",
        revision_order=1,
        public_time_precision="timestamp",
        published_at=datetime(2024, 4, 1, 12, tzinfo=timezone.utc),
        source_timezone="Asia/Shanghai",
        observed_at=datetime(2026, 10, 12, 2, tzinfo=timezone.utc),
        provenance="https://source.example/financial/600000/2024Q1",
    )
    coverage = FinancialCoverageInput(
        domain="financial_statement",
        source="ths",
        market="CN",
        symbol="600000",
        report_period_start=date(2024, 3, 31),
        report_period_end=date(2024, 3, 31),
        public_history_from=datetime(2024, 1, 1, tzinfo=timezone.utc),
        complete_through=datetime(2025, 1, 1, tzinfo=timezone.utc),
        source_timezone="Asia/Shanghai",
        evidence_uri="https://source.example/history-completeness/600000",
    )
    ingest_financial_versions(engine, [record])
    declare_financial_pit_coverage(engine, coverage)
    yield engine
    app.dependency_overrides.pop(get_warehouse_engine, None)
    engine.dispose()


async def test_financial_as_of_http_reads_version_storage_not_latest_dwd(
    pit_warehouse, test_client, test_user_token
):
    response = await test_client.get(
        "/api/v1/data/equity/financial_statement",
        params={
            "symbols": "600000",
            "start": "2024-03-31",
            "end": "2024-03-31",
            "source": "ths",
            "market": "CN",
            "as_of": "2024-04-02T00:00:00+00:00",
        },
        headers={"Authorization": f"Bearer {test_user_token}"},
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["rows"][0]["value"] == 10.0
    assert data["rows"][0]["revision"] == 1
    assert data["rows"][0]["source"] == "ths"
    assert data["pit"]["as_of"] == "2024-04-02T00:00:00+00:00"
    assert data["pit"]["coverage"][0]["evidence_uri"].endswith("600000")


async def test_financial_as_of_http_returns_explicit_unsupported_for_missing_coverage(
    pit_warehouse, test_client, test_user_token
):
    response = await test_client.get(
        "/api/v1/data/equity/financial_statement",
        params={
            "symbols": "600001",
            "start": "2024-03-31",
            "end": "2024-03-31",
            "source": "ths",
            "market": "CN",
            "as_of": "2024-04-02",
        },
        headers={"Authorization": f"Bearer {test_user_token}"},
    )

    assert response.status_code == 409
    assert response.json()["detail"].startswith("PIT_COVERAGE_GAP:")
