"""Offline scope checks for native model domains in the alert matrix."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import Column, Date, MetaData, String, Table, create_engine, insert

from opendata.data.capability import Capability
from opendata.data.domains import DomainSpec, dwd_table, ods_table
from opendata.data.providers import register_providers
from opendata.pipeline import alert_matrix
from opendata.pipeline.freshness import FreshnessReport

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

EXPECTED = date(2026, 10, 7)
_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_REGISTRY_SNAPSHOT_PATH = Path(__file__).parent / "fixtures" / "registry_batch5_snapshot.json"
_SQLALCHEMY_PARAMETER_LOG_FORMATS = {
    "[%s] %r",
    "[%s] [SQL parameters hidden due to hide_parameters=True]",
    "[raw sql] %r",
}
_SQLALCHEMY_TRANSACTION_LOG_LINES = {
    "BEGIN (implicit)",
    "BEGIN (implicit; DBAPI should not BEGIN due to autocommit mode)",
    "BEGIN TWOPHASE (implicit)",
    "COMMIT",
    "COMMIT using DBAPI connection.commit(), has no effect due to autocommit mode",
    "ROLLBACK",
    "ROLLBACK using DBAPI connection.rollback(); set skip_autocommit_rollback to prevent fully",
    "ROLLBACK will be skipped by skip_autocommit_rollback",
}
_FRESHNESS_MAX_QUERY = re.compile(
    r"\s*SELECT\s+MAX\s*\(\s*`([^`\r\n]+)`\s*\)\s+FROM\s+`([^`\r\n]+)`\s*",
    re.IGNORECASE,
)
_SQL_FORBIDDEN_MARKERS = (";", "--", "/*", "*/", "#")


def _registry_snapshot() -> dict[str, Any]:
    snapshot = json.loads(_REGISTRY_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    for relative_path, expected_hash in snapshot["source_hashes_sha256"].items():
        source_path = _REPOSITORY_ROOT / relative_path
        assert sha256(source_path.read_bytes()).hexdigest() == expected_hash, relative_path
    return snapshot


class _SqlQueryLogHandler(logging.Handler):
    def __init__(self, statements: list[str]) -> None:
        super().__init__(level=logging.INFO)
        self.statements = statements

    def emit(self, record: logging.LogRecord) -> None:
        if (
            record.args
            and isinstance(record.msg, str)
            and record.msg in _SQLALCHEMY_PARAMETER_LOG_FORMATS
        ):
            return
        if (
            not record.args
            and isinstance(record.msg, str)
            and record.msg in _SQLALCHEMY_TRANSACTION_LOG_LINES
        ):
            return
        statement = record.getMessage().strip()
        if statement:
            self.statements.append(statement)


def _freshness_max_query_parts(statement: str) -> tuple[str, str] | None:
    """Parse only the exact read shape emitted by ``freshness.py``.

    This intentionally is not a general SQL read-only check. The freshness
    caller emits one aggregate over one quoted column and table, so any other
    statement shape, SQL comment, or statement separator is rejected.
    """
    if any(marker in statement for marker in _SQL_FORBIDDEN_MARKERS):
        return None
    match = _FRESHNESS_MAX_QUERY.fullmatch(statement)
    if match is None:
        return None
    column, table = match.groups()
    if not column.strip() or not table.strip():
        return None
    return column, table


class _StatementCapture(list[str]):
    def __init__(self, logging_name: str) -> None:
        super().__init__()
        self._logger = logging.getLogger(f"sqlalchemy.engine.Engine.{logging_name}")
        self._handler = _SqlQueryLogHandler(self)
        self._previous_level = self._logger.level
        self._previous_propagate = self._logger.propagate
        self._logger.addHandler(self._handler)
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False

    def restore(self) -> None:
        self._logger.removeHandler(self._handler)
        self._logger.setLevel(self._previous_level)
        self._logger.propagate = self._previous_propagate


@pytest.fixture(autouse=True)
def registered_capabilities(monkeypatch: pytest.MonkeyPatch) -> None:
    """Populate the metadata registry and make provider I/O a hard failure."""
    register_providers()

    from opendata.data.providers.bls.models import _client as bls_client
    from opendata.data.providers.fmp.models import _client as fmp_client
    from opendata.data.providers.fred.models import _client as fred_client

    def refuse_io(*_args: object, **_kwargs: object) -> Any:
        pytest.fail("alert-matrix scope discovery must not call provider I/O")

    monkeypatch.setattr(fred_client, "_http_get", refuse_io)
    monkeypatch.setattr(bls_client, "_http_get", refuse_io)
    monkeypatch.setattr(bls_client, "_http_post_json", refuse_io)
    monkeypatch.setattr(fmp_client, "get_shared_http_client", refuse_io)


def _empty_engine() -> tuple[Engine, list[str]]:
    logging_name = f"alert-matrix-{uuid4().hex}"
    statements = _StatementCapture(logging_name)
    try:
        engine = create_engine("sqlite://", echo=True, logging_name=logging_name)
    except BaseException:
        statements.restore()
        raise
    return engine, statements


def _dispose_empty_engine(engine: Engine, statements: list[str]) -> None:
    try:
        if isinstance(statements, _StatementCapture):
            statements.restore()
    finally:
        engine.dispose()


def test_sql_log_capture_counts_statements_and_skips_only_known_metadata() -> None:
    statements: list[str] = []
    handler = _SqlQueryLogHandler(statements)
    messages = (
        ("SELECT * FROM probe", ()),
        ("INSERT INTO probe VALUES (1)", ()),
        ("UPDATE probe SET value = 2", ()),
        ("DELETE FROM probe", ()),
        ("CREATE TABLE probe (value INTEGER)", ()),
        ("PRAGMA table_info(probe)", ()),
        ("[%s] %r", ("generated in 0.0001s", {"value": 1})),
        ("BEGIN (implicit)", ()),
        ("unrecognized log record %s", ("is retained",)),
    )
    for message, args in messages:
        handler.emit(logging.LogRecord("probe", logging.INFO, "probe.py", 1, message, args, None))

    assert statements == [
        "SELECT * FROM probe",
        "INSERT INTO probe VALUES (1)",
        "UPDATE probe SET value = 2",
        "DELETE FROM probe",
        "CREATE TABLE probe (value INTEGER)",
        "PRAGMA table_info(probe)",
        "unrecognized log record is retained",
    ]
    assert _freshness_max_query_parts("SELECT MAX(`trade_date`) FROM `dwd_stock_daily`") == (
        "trade_date",
        "dwd_stock_daily",
    )
    assert _freshness_max_query_parts(
        " \tSELECT\n MAX ( `日期` )\tFROM\n `ods_stock_daily_akshare`  "
    ) == ("日期", "ods_stock_daily_akshare")
    assert not _freshness_max_query_parts("SELECT * FROM probe")
    assert not _freshness_max_query_parts("WITH x AS (SELECT 1) DELETE FROM probe WHERE id=1")
    assert not _freshness_max_query_parts("SELECT 1; DELETE FROM probe")
    assert not _freshness_max_query_parts("SELECT MAX(`trade_date`) FROM `dwd_stock_daily`;")
    assert not _freshness_max_query_parts("SELECT MAX(`trade;date`) FROM `dwd_stock_daily`")
    assert not _freshness_max_query_parts(
        "SELECT MAX(`trade_date`) FROM `dwd_stock_daily` -- harmless comment"
    )
    assert not _freshness_max_query_parts(
        "SELECT /* comment */ MAX(`trade_date`) FROM `dwd_stock_daily`"
    )
    assert not _freshness_max_query_parts(
        "SELECT MAX(`trade_date`) FROM `dwd_stock_daily` /* trailing comment */"
    )
    assert not _freshness_max_query_parts("INSERT INTO probe VALUES (1)")
    assert not _freshness_max_query_parts("UPDATE probe SET value = 2")
    assert not _freshness_max_query_parts("DELETE FROM probe")
    assert not _freshness_max_query_parts("CREATE TABLE probe (value INTEGER)")
    assert not _freshness_max_query_parts("PRAGMA table_info(probe)")
    assert not _freshness_max_query_parts("unrecognized statement")


def _create_stock_probe_tables(engine: Engine, statements: list[str]) -> None:
    metadata = MetaData()
    dwd = Table(
        dwd_table("stock_daily"),
        metadata,
        Column("symbol", String, primary_key=True),
        Column("trade_date", Date, primary_key=True),
    )
    ods_akshare = Table(
        ods_table("stock_daily", "akshare"),
        metadata,
        Column("symbol", String, primary_key=True),
        Column("日期", Date, primary_key=True),
    )
    ods_ths = Table(
        ods_table("stock_daily", "ths"),
        metadata,
        Column("symbol", String, primary_key=True),
        Column("trade_date", Date, primary_key=True),
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(insert(dwd), {"symbol": "PROBE", "trade_date": date(2026, 10, 6)})
        connection.execute(insert(ods_akshare), {"symbol": "PROBE", "日期": date(2026, 10, 6)})
        connection.execute(insert(ods_ths), {"symbol": "PROBE", "trade_date": date(2026, 10, 6)})
    statements.clear()


def test_default_scope_accounts_for_all_domains_legs_and_only_queries_legacy_tables() -> None:
    snapshot = _registry_snapshot()
    engine, statements = _empty_engine()
    try:
        legs = alert_matrix.registered_legs()
        reports, scope = alert_matrix.collect_freshness(engine, expected=EXPECTED)
        tables = alert_matrix.warehouse_tables()
        from opendata.data.providers.catalog import list_providers
        from opendata.data.registry import get_registry

        expected_domain_ids = set(snapshot["domain_ids"])
        expected_models = snapshot["provider_model_identities"]
        expected_model_domains = {
            (model["source"], model["model"], model["domain"]) for model in expected_models
        }
        expected_static_bindings = {
            (model["source"], model["model"]): model["binding"] for model in expected_models
        }
        declared_static_bindings = {
            (provider.source, model_id): f"{binding.module}:{binding.class_name}"
            for provider in list_providers()
            for binding in provider.fetcher_bindings
            for model_id in (binding.canonical_model_ids or (binding.model_key,))
        }
        actual_model_domains = {
            (descriptor.source, descriptor.model, descriptor.domain)
            for descriptor in get_registry().list_model_descriptors()
        }
        actual_legs = {(domain, source) for domain, sources in legs.items() for source in sources}
        expected_legs = {
            (item["domain"], item["source"]) for item in snapshot["source_domain_legs"]
        }
        expected_deferred_legs = {
            (item["domain"], item["source"]) for item in snapshot["deferred_source_domain_legs"]
        }
        expected_deferred = tuple(snapshot["deferred_domain_ids"])
        expected_query_domains = set(snapshot["query_semantic_domain_ids"])
        expected_bound_query_domains = {
            domain
            for _source, _model, domain in expected_model_domains
            if domain in expected_query_domains
        }
        expected_legacy_domains = set(snapshot["legacy_dwd_domain_ids"])
        expected_registered_model_domains = {
            (source, model, domain)
            for source, model, domain in expected_model_domains
            if domain in expected_deferred
        }
        expected_descriptor_model_domains = {
            (model["source"], model["model"], model["domain"])
            for model in expected_models
            if model["canonical_ids_declared"]
        }
        expected_legacy_model_domains = expected_model_domains - expected_registered_model_domains
        expected_legacy_model_legs = {
            (domain, source) for source, _model, domain in expected_legacy_model_domains
        }
        expected_legacy_legs = {
            (item["domain"], item["source"]) for item in snapshot["legacy_source_domain_legs"]
        }
        expected_mapped_ods = {
            (item["domain"], item["source"]) for item in snapshot["mapped_ods_legs"]
        }
        expected_report_identities = {(domain, None) for domain in expected_legacy_domains}
        expected_report_identities.update(expected_mapped_ods)
        expected_dwd_tables = {dwd_table(domain) for domain in expected_legacy_domains}
        expected_catalog_ods_tables = {
            ods_table(domain, source) for domain, source in expected_legacy_legs
        }
        expected_measured_ods_tables = {
            ods_table(domain, source) for domain, source in expected_mapped_ods
        }
        expected_warehouse_tables = expected_dwd_tables | expected_catalog_ods_tables

        assert expected_bound_query_domains == set(expected_deferred)
        assert expected_legacy_domains | set(expected_deferred) == expected_domain_ids
        assert not expected_legacy_domains & set(expected_deferred)
        assert expected_deferred_legs | expected_legacy_legs == expected_legs
        assert expected_deferred_legs.isdisjoint(expected_legacy_legs)
        assert declared_static_bindings == expected_static_bindings
        assert (
            len(expected_descriptor_model_domains)
            == snapshot["counts"]["descriptor_eligible_identity_count"]
        )
        assert actual_model_domains == expected_descriptor_model_domains
        assert {
            identity for identity in actual_model_domains if identity[2] in expected_deferred
        } == expected_registered_model_domains
        assert expected_legacy_model_legs == expected_legacy_legs
        assert len(expected_models) == 372
        assert len(legs) == len(expected_domain_ids) == 359
        assert set(legs) == expected_domain_ids
        assert actual_legs == expected_legs
        assert len(actual_legs) == 372
        assert set(snapshot["deferred_domain_ids"]) == expected_query_domains
        assert scope.domains == len(expected_legacy_domains) == 20
        assert scope.deferred_domains == expected_deferred
        assert scope.deferred_legs == len(expected_deferred_legs) == 339
        assert scope.source_legs == len(expected_mapped_ods) == 10
        assert scope.unmapped_legs == len(snapshot["unmapped_ods_legs"]) == 23
        assert scope.domains + len(scope.deferred_domains) == len(expected_domain_ids)
        assert scope.source_legs + scope.unmapped_legs + scope.deferred_legs == len(actual_legs)
        assert {
            report.domain for report in reports if report.source is None
        } == expected_legacy_domains
        assert {
            (report.domain, report.source) for report in reports if report.source is not None
        } == expected_mapped_ods
        assert {(report.domain, report.source) for report in reports} == expected_report_identities
        assert set(tables) == expected_warehouse_tables
        assert expected_warehouse_tables == expected_dwd_tables | expected_catalog_ods_tables
        assert expected_catalog_ods_tables.isdisjoint(expected_dwd_tables)
        parsed_queries = [_freshness_max_query_parts(statement) for statement in statements]
        assert all(query is not None for query in parsed_queries)
        queried_tables = {query[1] for query in parsed_queries if query is not None}
        assert len(queried_tables) == len(statements) == len(reports) == 30
        assert queried_tables == expected_dwd_tables | expected_measured_ods_tables
        assert expected_measured_ods_tables.isdisjoint(expected_dwd_tables)
        scope_dict = scope.as_dict()
        assert scope_dict["deferred_domains"] == list(expected_deferred)
        assert scope_dict["deferred_legs"] == 339
    finally:
        _dispose_empty_engine(engine, statements)


def test_stock_is_still_measured_when_bls_series_is_selected_with_it() -> None:
    engine, statements = _empty_engine()
    _create_stock_probe_tables(engine, statements)
    try:
        reports, scope = alert_matrix.collect_freshness(
            engine,
            expected=EXPECTED,
            domains=("bls_series", "stock_daily"),
        )

        assert {report.domain for report in reports} == {"stock_daily"}
        assert len(reports) == 3
        assert all(report.status == "stale" for report in reports)
        assert scope.domains == 1
        assert scope.source_legs == 2
        assert scope.unmapped_legs == 0
        assert scope.deferred_domains == ("bls_series",)
        assert scope.deferred_legs == 1
        assert len(statements) == 3
        assert all("bls_series" not in statement for statement in statements)
    finally:
        _dispose_empty_engine(engine, statements)


def test_native_only_selection_has_no_warehouse_reads_or_false_freshness_alerts() -> None:
    engine, statements = _empty_engine()
    try:
        reports, scope = alert_matrix.collect_freshness(
            engine, expected=EXPECTED, domains=("bls_series",)
        )
        tables = alert_matrix.warehouse_tables(("bls_series",))
        plans, partitioned = alert_matrix.collect_partitions(
            engine, current_year=2026, domains=("bls_series",)
        )
        run = asyncio.run(
            alert_matrix.run_alert_matrix(
                engine,
                expected=EXPECTED,
                domains=("bls_series",),
                current_year=2026,
            )
        )

        assert reports == ()
        assert scope.domains == scope.source_legs == scope.unmapped_legs == 0
        assert scope.deferred_domains == ("bls_series",)
        assert scope.deferred_legs == 1
        assert tables == ()
        assert plans == {}
        assert partitioned == 0
        assert run.reports == ()
        assert run.alerts == ()
        assert run.delivered == 0
        assert run.scope.as_dict()["deferred_domains"] == ["bls_series"]
        assert run.scope.as_dict()["deferred_legs"] == 1
        assert statements == []
    finally:
        _dispose_empty_engine(engine, statements)


def test_semantics_alone_do_not_defer_a_reviewed_domain_without_canonical_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opendata.data.domains as domains_module
    import opendata.data.registry as registry_module

    domain = "reviewed_legacy_fixture"
    spec = DomainSpec(
        display_name="Reviewed legacy fixture",
        rest_path="test/reviewed-legacy",
        contract="Bar",
        temporal_kind="series",
        time_field="trade_date",
        natural_key=("symbol", "trade_date"),
        filter_dims=("symbol",),
        storage_mode="upsert",
        permissions=("query", "store"),
    )
    capability = Capability(
        asset_class="equity",
        domain=domain,
        period="1D",
        market="offline",
        source="fixture",
        verified=False,
    )

    class RegistryWithoutModelDescriptor:
        def capabilities(self) -> list[Capability]:
            return [capability]

        def list_model_descriptors(self) -> tuple[()]:
            return ()

    registry = RegistryWithoutModelDescriptor()
    monkeypatch.setattr(alert_matrix, "load_domains", lambda: {domain: spec})
    monkeypatch.setattr(domains_module, "require_domain_semantics", lambda _domain: spec)
    monkeypatch.setattr(registry_module, "get_registry", lambda: registry)
    measured: list[tuple[str, str | None]] = []

    def fake_dwd(_engine: object, requested_domain: str, *, expected: date) -> FreshnessReport:
        measured.append((requested_domain, None))
        return FreshnessReport(
            requested_domain, None, "trade_date", None, expected, None, "missing"
        )

    def fake_ods(
        _engine: object,
        requested_domain: str,
        source: str,
        *,
        expected: date,
    ) -> FreshnessReport:
        measured.append((requested_domain, source))
        return FreshnessReport(
            requested_domain, source, "trade_date", None, expected, None, "missing"
        )

    monkeypatch.setattr(alert_matrix, "dwd_freshness", fake_dwd)
    monkeypatch.setattr(alert_matrix, "_ods_leg", fake_ods)

    reports, scope = alert_matrix.collect_freshness(
        cast("Any", object()), expected=EXPECTED, domains=(domain,)
    )

    assert spec.semantics_declared is True
    assert measured == [(domain, None), (domain, "fixture")]
    assert len(reports) == 2
    assert scope.domains == 1
    assert scope.source_legs == 1
    assert scope.deferred_domains == ()
    assert scope.deferred_legs == 0


@pytest.mark.parametrize(
    "collector",
    ["freshness", "tables", "partitions"],
)
def test_unknown_domain_is_still_rejected_without_warehouse_io(
    collector: str,
) -> None:
    engine, statements = _empty_engine()
    try:
        with pytest.raises(ValueError, match="not registered"):
            if collector == "freshness":
                alert_matrix.collect_freshness(
                    engine, expected=EXPECTED, domains=("not_registered",)
                )
            elif collector == "tables":
                alert_matrix.warehouse_tables(("not_registered",))
            else:
                alert_matrix.collect_partitions(
                    engine, current_year=2026, domains=("not_registered",)
                )
        assert statements == []
    finally:
        _dispose_empty_engine(engine, statements)
