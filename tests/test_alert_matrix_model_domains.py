"""Offline scope checks for native model domains in the alert matrix."""

from __future__ import annotations

import asyncio
from datetime import date
from typing import TYPE_CHECKING, Any, cast

import pytest
from sqlalchemy import Column, Date, MetaData, String, Table, create_engine, event, insert, pool

from opendata.data.capability import Capability
from opendata.data.domains import DomainSpec, dwd_table, ods_table
from opendata.data.providers import register_providers
from opendata.pipeline import alert_matrix
from opendata.pipeline.freshness import FreshnessReport

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

EXPECTED = date(2026, 10, 7)
NATIVE_MODEL_DOMAINS = (
    "balance_of_payments",
    "bls_search",
    "bls_series",
    "currency_reference_rates",
    "equity_historical",
    "equity_quote",
    "fred_search",
    "fred_series",
    "sofr",
    "sonia",
    "yield_curve",
)


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
    engine = create_engine(
        "sqlite://", poolclass=pool.StaticPool, connect_args={"check_same_thread": False}
    )
    statements: list[str] = []

    def capture(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    return engine, statements


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
    engine, statements = _empty_engine()
    try:
        legs = alert_matrix.registered_legs()
        reports, scope = alert_matrix.collect_freshness(engine, expected=EXPECTED)
        tables = alert_matrix.warehouse_tables()

        assert len(legs) == 31
        assert sum(len(sources) for sources in legs.values()) == 44
        assert scope.domains == 20
        assert scope.deferred_domains == NATIVE_MODEL_DOMAINS
        assert scope.deferred_legs == 11
        assert scope.source_legs + scope.unmapped_legs == 33
        assert scope.domains + len(scope.deferred_domains) == 31
        assert scope.source_legs + scope.unmapped_legs + scope.deferred_legs == 44
        assert len(reports) == scope.domains + scope.source_legs
        assert len(statements) == len(reports) == 30
        assert all(
            f"{prefix}_{domain}" not in statement
            for domain in NATIVE_MODEL_DOMAINS
            for prefix in ("dwd", "ods")
            for statement in statements
        )
        assert all(
            not table.startswith((f"dwd_{domain}", f"ods_{domain}_"))
            for domain in NATIVE_MODEL_DOMAINS
            for table in tables
        )
        scope_dict = scope.as_dict()
        assert scope_dict["deferred_domains"] == list(NATIVE_MODEL_DOMAINS)
        assert scope_dict["deferred_legs"] == 11
    finally:
        engine.dispose()


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
        engine.dispose()


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
        engine.dispose()


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
        engine.dispose()
