"""Tests for the authenticated, bounded financial archive REST slice."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.exc import SQLAlchemyError

from opendata.api import api_router, api_v2_router
from opendata.api import financial_observation_archive as archive_api
from opendata.api.dependencies import get_current_principal
from opendata.data.financial_observation_archive_reader import (
    FinancialObservationArchivePage,
    FinancialObservationArchiveRow,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


def _principal(*, allowed: bool) -> SimpleNamespace:
    """Build a narrow principal test double for domain-scope checks."""
    return SimpleNamespace(allows_domain=lambda domain: allowed and domain == "financial_statement")


def _row(
    *, item_code: str = "revenue", value: str | None = "123.4500"
) -> FinancialObservationArchiveRow:
    """Return one source-native row with distinct report, disclosure, and observation times."""
    return FinancialObservationArchiveRow(
        observation_id="a" * 64,
        natural_key_sha256="b" * 64,
        content_sha256="c" * 64,
        source_id="ths_fuyao",
        instrument_id="600519.SH",
        statement_type="income",
        periodicity="quarterly",
        report_period=date(2025, 3, 31),
        source_disclosure_date=date(2025, 4, 25),
        observed_at_utc=datetime(2025, 4, 25, 3, 4, 5, 6000, tzinfo=timezone.utc),
        item_code=item_code,
        value_decimal_text=value,
        native_unit="CNY",
        source_metadata_json='{"omitted":"from public response"}',
        observation_policy="current_as_observed_only",
    )


def _request_kwargs(principal: object) -> dict[str, Any]:
    """Return the required route arguments for one bounded report-period request."""
    return {
        "request": Request({"type": "http", "query_string": b""}),
        "principal": principal,
        "source_id": "ths_fuyao",
        "instrument_id": "600519.SH",
        "statement_type": "income",
        "periodicity": "quarterly",
        "start_report_period": date(2025, 3, 31),
        "end_report_period": date(2025, 3, 31),
        "page_size": 100,
    }


def _reader_kwargs() -> dict[str, Any]:
    """Return only archive-reader inputs, excluding route-only request/auth values."""
    query = _request_kwargs(_principal(allowed=True))
    query.pop("request")
    query.pop("principal")
    return query


def _configure_dedicated_archive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set an isolated archive URL and non-overlapping application database targets."""
    monkeypatch.setattr(
        archive_api.settings,
        "financial_archive_database_url",
        SecretStr("mysql+pymysql://reader:secret@archive.internal:3307/financial_archive"),
    )
    monkeypatch.setattr(archive_api.settings, "mysql_host", "main.internal")
    monkeypatch.setattr(archive_api.settings, "mysql_port", 3306)
    monkeypatch.setattr(archive_api.settings, "mysql_database", "opendata")
    monkeypatch.setattr(archive_api.settings, "data_mysql_host", "warehouse.internal")
    monkeypatch.setattr(archive_api.settings, "data_mysql_port", 3306)
    monkeypatch.setattr(archive_api.settings, "data_mysql_database", "opendata_data")


class _FakeTransaction:
    """Track the archive transaction lifecycle without opening a database."""

    def __init__(self, events: list[object]) -> None:
        self.events = events

    def __enter__(self) -> _FakeTransaction:
        self.events.append("transaction_enter")
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        self.events.append(("transaction_exit", exc_type))
        return False


class _FakeConnection:
    """Record isolation and transaction setup expected by the archive reader."""

    def __init__(self, events: list[object]) -> None:
        self.events = events

    def execution_options(self, **options: object) -> _FakeConnection:
        self.events.append(("execution_options", options))
        return self

    def begin(self) -> _FakeTransaction:
        self.events.append("transaction_begin")
        return _FakeTransaction(self.events)


class _FakeConnectionContext:
    """Track one connection scope and its exit behavior."""

    def __init__(self, events: list[object]) -> None:
        self.events = events
        self.connection = _FakeConnection(events)

    def __enter__(self) -> _FakeConnection:
        self.events.append("connection_enter")
        return self.connection

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        self.events.append(("connection_exit", exc_type))
        return False


class _FakeEngine:
    """Record connection acquisition and pool disposal for sync reader tests."""

    def __init__(self, events: list[object]) -> None:
        self.events = events

    def connect(self) -> _FakeConnectionContext:
        self.events.append("engine_connect")
        return _FakeConnectionContext(self.events)

    def dispose(self) -> None:
        self.events.append("engine_dispose")


def test_scope_denial_happens_before_archive_engine_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A principal without financial scope must be rejected before engine creation."""
    engine_calls = 0

    def unexpected_engine() -> Engine:
        nonlocal engine_calls
        engine_calls += 1
        pytest.fail("archive engine must not be created for a denied principal")

    monkeypatch.setattr(archive_api, "_archive_engine_from_settings", unexpected_engine)
    with pytest.raises(HTTPException) as raised:
        asyncio.run(
            archive_api.query_financial_observation_archive(
                **_request_kwargs(_principal(allowed=False))
            )
        )

    assert raised.value.status_code == 403
    assert engine_calls == 0


def test_missing_archive_url_returns_503_without_default_database_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unset archive URL fails closed without trying the application database."""
    monkeypatch.setattr(archive_api.settings, "financial_archive_database_url", None)
    engine_calls = 0

    def unexpected_create_engine(*args: object, **kwargs: object) -> Engine:
        nonlocal engine_calls
        engine_calls += 1
        pytest.fail("missing archive configuration must fail before any engine is created")

    monkeypatch.setattr(archive_api, "create_engine", unexpected_create_engine)
    with pytest.raises(HTTPException) as raised:
        asyncio.run(
            archive_api.query_financial_observation_archive(
                **_request_kwargs(_principal(allowed=True))
            )
        )

    assert raised.value.status_code == 503
    assert engine_calls == 0


def test_malformed_archive_url_returns_sanitized_503_without_engine_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed archive configuration fails closed before create_engine."""
    monkeypatch.setattr(
        archive_api.settings,
        "financial_archive_database_url",
        SecretStr("definitely not a SQLAlchemy URL"),
    )
    engine_calls = 0

    def unexpected_create_engine(*args: object, **kwargs: object) -> Engine:
        nonlocal engine_calls
        engine_calls += 1
        pytest.fail("malformed configuration must not reach create_engine")

    monkeypatch.setattr(archive_api, "create_engine", unexpected_create_engine)
    with pytest.raises(HTTPException) as raised:
        asyncio.run(
            archive_api.query_financial_observation_archive(
                **_request_kwargs(_principal(allowed=True))
            )
        )

    assert raised.value.status_code == 503
    assert engine_calls == 0


def test_blank_archive_url_returns_503_without_engine_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whitespace-only archive configuration is treated as unset."""
    monkeypatch.setattr(
        archive_api.settings,
        "financial_archive_database_url",
        SecretStr("   "),
    )
    monkeypatch.setattr(
        archive_api,
        "create_engine",
        lambda *args, **kwargs: pytest.fail("blank configuration must not create an engine"),
    )

    with pytest.raises(archive_api.FinancialArchiveUnavailableError) as raised:
        archive_api._archive_engine_from_settings()

    assert str(raised.value) == "financial archive URL is not configured"


def test_non_mysql_archive_url_is_rejected_before_engine_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The archive reader cannot be pointed at an implicit SQLite or other dialect."""
    monkeypatch.setattr(
        archive_api.settings,
        "financial_archive_database_url",
        SecretStr("sqlite:///financial_archive.db"),
    )
    monkeypatch.setattr(
        archive_api,
        "create_engine",
        lambda *args, **kwargs: pytest.fail("non-MySQL URL must not create an engine"),
    )

    with pytest.raises(archive_api.FinancialArchiveUnavailableError) as raised:
        archive_api._archive_engine_from_settings()

    assert str(raised.value) == "financial archive requires an explicit MySQL URL"


@pytest.mark.parametrize(
    "application_url",
    [
        "malformed application URL",
        "mysql+pymysql://warehouse@/opendata_data?unix_socket=%2Fvar%2Flib%2Fmysql.sock",
    ],
    ids=["malformed", "unix-socket-target"],
)
def test_uncomparable_application_mysql_target_fails_closed_before_engine_creation(
    monkeypatch: pytest.MonkeyPatch,
    application_url: str,
) -> None:
    """Unknown or socket-routed app targets cannot be skipped during isolation checks."""
    archive_url = SecretStr("mysql+pymysql://reader:secret@archive.internal:3307/financial_archive")
    monkeypatch.setattr(
        archive_api,
        "settings",
        SimpleNamespace(
            financial_archive_database_url=archive_url,
            database_url_sync=application_url,
            data_database_url="mysql+pymysql://warehouse@warehouse.internal/opendata_data",
        ),
    )
    engine_calls = 0

    def unexpected_engine(*args: object, **kwargs: object) -> Engine:
        nonlocal engine_calls
        engine_calls += 1
        pytest.fail("uncomparable application target must not create an archive engine")

    monkeypatch.setattr(archive_api, "create_engine", unexpected_engine)

    with pytest.raises(archive_api.FinancialArchiveUnavailableError) as raised:
        archive_api._archive_engine_from_settings()

    assert str(raised.value) == "financial archive configuration is invalid"
    assert "warehouse.internal" not in str(raised.value)
    assert "unix_socket" not in str(raised.value)
    assert engine_calls == 0


def test_archive_unix_socket_url_is_rejected_before_engine_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An archive socket URL has an ambiguous endpoint and is never passed to a driver."""
    _configure_dedicated_archive(monkeypatch)
    monkeypatch.setattr(
        archive_api.settings,
        "financial_archive_database_url",
        SecretStr(
            "mysql+pymysql://reader:secret@archive.internal:3307/financial_archive"
            "?unix_socket=%2Fvar%2Flib%2Fmysql.sock"
        ),
    )
    engine_calls = 0

    def unexpected_engine(*args: object, **kwargs: object) -> Engine:
        nonlocal engine_calls
        engine_calls += 1
        pytest.fail("socket-routed archive URL must not create an engine")

    monkeypatch.setattr(archive_api, "create_engine", unexpected_engine)

    with pytest.raises(archive_api.FinancialArchiveUnavailableError) as raised:
        archive_api._archive_engine_from_settings()

    assert str(raised.value) == "financial archive requires an explicit MySQL URL"
    assert "secret" not in str(raised.value)
    assert engine_calls == 0


def test_authentication_failure_returns_401_before_archive_engine_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected authentication dependency cannot create an archive engine."""
    engine_calls = 0

    def unexpected_engine() -> Engine:
        nonlocal engine_calls
        engine_calls += 1
        pytest.fail("unauthenticated requests must not create an archive engine")

    async def reject_authentication() -> None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    monkeypatch.setattr(archive_api, "_archive_engine_from_settings", unexpected_engine)
    app = FastAPI()
    app.include_router(archive_api.router, prefix="/api/v2/data")
    app.dependency_overrides[get_current_principal] = reject_authentication

    with TestClient(app) as client:
        response = client.get(
            "/api/v2/data/financial-statements/archive",
            params={
                "source_id": "ths_fuyao",
                "instrument_id": "600519.SH",
                "statement_type": "income",
                "periodicity": "quarterly",
                "start_report_period": "2025-03-31",
                "end_report_period": "2025-03-31",
            },
        )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"
    assert response.headers["cache-control"] == "private, no-store"
    assert engine_calls == 0


def test_archive_url_matching_application_schema_is_rejected_before_engine_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dedicated archive setting cannot point to either application schema."""
    monkeypatch.setattr(
        archive_api.settings,
        "financial_archive_database_url",
        SecretStr("mysql+pymysql://reader:secret@db.internal:3307/archive_db"),
    )
    monkeypatch.setattr(archive_api.settings, "mysql_host", "db.internal")
    monkeypatch.setattr(archive_api.settings, "mysql_port", 3307)
    monkeypatch.setattr(archive_api.settings, "mysql_database", "archive_db")
    monkeypatch.setattr(archive_api.settings, "data_mysql_host", "warehouse.internal")
    monkeypatch.setattr(archive_api.settings, "data_mysql_port", 3306)
    monkeypatch.setattr(archive_api.settings, "data_mysql_database", "opendata_data")
    engine_calls = 0

    def unexpected_create_engine(*args: object, **kwargs: object) -> Engine:
        nonlocal engine_calls
        engine_calls += 1
        pytest.fail("an application database target must be rejected before engine creation")

    monkeypatch.setattr(archive_api, "create_engine", unexpected_create_engine)
    with pytest.raises(archive_api.FinancialArchiveUnavailableError) as raised:
        archive_api._archive_engine_from_settings()

    assert "secret" not in str(raised.value)
    assert "db.internal" not in str(raised.value)
    assert engine_calls == 0

    monkeypatch.setattr(
        archive_api.settings,
        "financial_archive_database_url",
        SecretStr("mysql+pymysql://reader:secret@warehouse.internal:3306/opendata_data"),
    )
    with pytest.raises(archive_api.FinancialArchiveUnavailableError) as warehouse_raised:
        archive_api._archive_engine_from_settings()

    assert "secret" not in str(warehouse_raised.value)
    assert "warehouse.internal" not in str(warehouse_raised.value)
    assert engine_calls == 0


@pytest.mark.parametrize(
    ("archive_host", "application_host"),
    [
        ("localhost", "127.0.0.1"),
        ("127.0.0.1", "localhost"),
        ("localhost.", "127.0.0.1"),
    ],
)
def test_loopback_aliases_of_application_schema_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
    archive_host: str,
    application_host: str,
) -> None:
    """Literal loopback spellings cannot bypass same-schema detection."""
    monkeypatch.setattr(
        archive_api.settings,
        "financial_archive_database_url",
        SecretStr(f"mysql+pymysql://reader:secret@{archive_host}:3306/opendata"),
    )
    monkeypatch.setattr(archive_api.settings, "mysql_host", application_host)
    monkeypatch.setattr(archive_api.settings, "mysql_port", 3306)
    monkeypatch.setattr(archive_api.settings, "mysql_database", "opendata")
    monkeypatch.setattr(archive_api.settings, "data_mysql_host", "warehouse.internal")
    monkeypatch.setattr(archive_api.settings, "data_mysql_port", 3306)
    monkeypatch.setattr(archive_api.settings, "data_mysql_database", "opendata_data")
    engine_calls = 0

    def unexpected_engine(*args: object, **kwargs: object) -> Engine:
        nonlocal engine_calls
        engine_calls += 1
        pytest.fail("loopback alias to an application schema must be rejected")

    monkeypatch.setattr(archive_api, "create_engine", unexpected_engine)
    with pytest.raises(archive_api.FinancialArchiveUnavailableError):
        archive_api._archive_engine_from_settings()

    assert engine_calls == 0


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("localhost", "<loopback>"),
        ("localhost.", "<loopback>"),
        ("api.localhost", "<loopback>"),
        ("127.0.0.1", "<loopback>"),
        ("::1", "<loopback>"),
        ("::ffff:127.0.0.1", "<loopback>"),
        ("DB.Example.", "db.example"),
    ],
)
def test_database_host_canonicalization_handles_literal_aliases(host: str, expected: str) -> None:
    """Canonicalization handles literal aliases only and leaves DNS names unresolved."""
    assert archive_api._canonical_database_host(host) == expected


def test_engine_creation_uses_only_the_dedicated_archive_url_and_null_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid dedicated URL builds one unpooled engine with bounded connect timeouts."""
    _configure_dedicated_archive(monkeypatch)
    engine = _FakeEngine([])
    create_engine_calls: list[tuple[object, dict[str, object]]] = []

    def create_archive_engine(url: object, **kwargs: object) -> _FakeEngine:
        create_engine_calls.append((url, kwargs))
        return engine

    monkeypatch.setattr(archive_api, "create_engine", create_archive_engine)
    actual_engine = archive_api._archive_engine_from_settings()

    assert actual_engine is engine
    assert len(create_engine_calls) == 1
    url, options = create_engine_calls[0]
    assert str(url).startswith("mysql+pymysql://reader:***@archive.internal:3307/financial_archive")
    assert options["poolclass"] is archive_api.NullPool
    assert options["connect_args"] == {
        "connect_timeout": 5,
        "read_timeout": 15,
        "write_timeout": 5,
    }


def test_engine_creation_errors_are_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Driver or engine construction diagnostics never include a DSN in the API error."""
    _configure_dedicated_archive(monkeypatch)

    def fail_engine_creation(*args: object, **kwargs: object) -> Engine:
        raise SQLAlchemyError(
            "mysql+pymysql://reader:secret@archive.internal:3307/financial_archive"
        )

    monkeypatch.setattr(archive_api, "create_engine", fail_engine_creation)
    with pytest.raises(archive_api.FinancialArchiveUnavailableError) as raised:
        archive_api._archive_engine_from_settings()

    assert str(raised.value) == "financial archive engine is unavailable"
    assert raised.value.__suppress_context__ is True


def test_sync_read_uses_repeatable_read_and_returns_only_the_bounded_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One connection and RR transaction surround the reader and public projection."""
    _configure_dedicated_archive(monkeypatch)
    events: list[object] = []
    engine = _FakeEngine(events)
    reader_args: dict[str, object] = {}

    def create_archive_engine(url: object, **kwargs: object) -> _FakeEngine:
        events.append(("create_engine", url))
        return engine

    def read_page(connection: object, **kwargs: object) -> FinancialObservationArchivePage:
        events.append("reader")
        reader_args.update(kwargs)
        return FinancialObservationArchivePage(rows=(_row(value="123.4500"),), next_cursor=None)

    monkeypatch.setattr(archive_api, "create_engine", create_archive_engine)
    monkeypatch.setattr(archive_api, "read_financial_observation_page", read_page)
    query = _reader_kwargs()
    query["page_size"] = 199

    payload = archive_api._read_archive_page(**query)

    assert [event for event in events if isinstance(event, str)] == [
        "engine_connect",
        "connection_enter",
        "transaction_begin",
        "transaction_enter",
        "reader",
        "engine_dispose",
    ]
    assert ("execution_options", {"isolation_level": "REPEATABLE READ"}) in events
    assert any(
        isinstance(event, tuple) and event[0] == "transaction_exit" and event[1] is None
        for event in events
    )
    assert any(
        isinstance(event, tuple) and event[0] == "connection_exit" and event[1] is None
        for event in events
    )
    assert reader_args == {
        "source_id": "ths_fuyao",
        "instrument_id": "600519.SH",
        "statement_type": "income",
        "periodicity": "quarterly",
        "start_report_period": date(2025, 3, 31),
        "end_report_period": date(2025, 3, 31),
        "page_size": 199,
    }
    assert payload["completeness"] == "unknown"
    assert payload["point_in_time_reconstruction"] == "unavailable"
    assert payload["records"] == [archive_api._row_payload(_row(value="123.4500"))]


def test_sync_read_failure_closes_connection_and_sanitizes_driver_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed SQL read rolls back/exits all scopes and returns no driver diagnostics."""
    _configure_dedicated_archive(monkeypatch)
    events: list[object] = []
    engine = _FakeEngine(events)

    monkeypatch.setattr(archive_api, "create_engine", lambda *args, **kwargs: engine)

    def fail_read(connection: object, **kwargs: object) -> FinancialObservationArchivePage:
        events.append("reader")
        raise SQLAlchemyError(
            "mysql+pymysql://reader:secret@archive.internal/financial_archive failed"
        )

    monkeypatch.setattr(archive_api, "read_financial_observation_page", fail_read)
    query = _reader_kwargs()

    with pytest.raises(archive_api.FinancialArchiveUnavailableError) as raised:
        archive_api._read_archive_page(**query)

    assert str(raised.value) == "financial archive is unavailable"
    assert raised.value.__suppress_context__ is True
    assert "secret" not in str(raised.value)
    assert "archive.internal" not in str(raised.value)
    assert any(
        isinstance(event, tuple) and event[0] == "transaction_exit" and event[1] is not None
        for event in events
    )
    assert any(
        isinstance(event, tuple) and event[0] == "connection_exit" and event[1] is not None
        for event in events
    )
    assert events[-1] == "engine_dispose"


def test_reader_contract_rejection_is_sanitized_after_transaction_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An invalid archive row contract is reported as unavailable after cleanup."""
    _configure_dedicated_archive(monkeypatch)
    events: list[object] = []
    engine = _FakeEngine(events)

    monkeypatch.setattr(archive_api, "create_engine", lambda *args, **kwargs: engine)

    def reject_read(connection: object, **kwargs: object) -> FinancialObservationArchivePage:
        events.append("reader")
        raise archive_api.FinancialObservationArchiveReadError("private archive row detail")

    monkeypatch.setattr(archive_api, "read_financial_observation_page", reject_read)
    query = _reader_kwargs()

    with pytest.raises(archive_api.FinancialArchiveUnavailableError) as raised:
        archive_api._read_archive_page(**query)

    assert str(raised.value) == "financial archive data is unavailable"
    assert "private archive row detail" not in str(raised.value)
    assert events[-1] == "engine_dispose"


def test_engine_disposal_failure_does_not_replace_a_successful_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A NullPool cleanup diagnostic is suppressed after a successful response read."""
    _configure_dedicated_archive(monkeypatch)
    events: list[object] = []

    class _FailingDisposeEngine(_FakeEngine):
        def dispose(self) -> None:
            self.events.append("engine_dispose")
            raise RuntimeError("private driver cleanup detail")

    engine = _FailingDisposeEngine(events)
    monkeypatch.setattr(archive_api, "create_engine", lambda *args, **kwargs: engine)
    monkeypatch.setattr(
        archive_api,
        "read_financial_observation_page",
        lambda connection, **kwargs: FinancialObservationArchivePage(rows=(), next_cursor=None),
    )
    query = _reader_kwargs()

    result = archive_api._read_archive_page(**query)

    assert result["records"] == []
    assert events[-1] == "engine_dispose"


def test_sync_read_rejects_has_more_page_and_exits_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SQL page sentinel becomes a 413 decision only after closing the snapshot."""
    _configure_dedicated_archive(monkeypatch)
    events: list[object] = []
    engine = _FakeEngine(events)

    monkeypatch.setattr(archive_api, "create_engine", lambda *args, **kwargs: engine)
    monkeypatch.setattr(
        archive_api,
        "read_financial_observation_page",
        lambda connection, **kwargs: FinancialObservationArchivePage(
            rows=(_row(),),
            next_cursor=object(),  # type: ignore[arg-type]
        ),
    )
    query = _reader_kwargs()

    with pytest.raises(archive_api.FinancialArchivePageTooLargeError):
        archive_api._read_archive_page(**query)

    assert any(
        isinstance(event, tuple)
        and event[0] == "transaction_exit"
        and event[1] is archive_api.FinancialArchivePageTooLargeError
        for event in events
    )
    assert any(
        isinstance(event, tuple)
        and event[0] == "connection_exit"
        and event[1] is archive_api.FinancialArchivePageTooLargeError
        for event in events
    )
    assert events[-1] == "engine_dispose"


def test_route_is_registered_only_on_the_v2_router() -> None:
    """The archive URL is versioned and is absent from legacy compatibility routers."""
    expected = "/api/v2/data/financial-statements/archive"

    assert any(route.path == expected for route in api_v2_router.routes)
    assert all(route.path != expected for route in api_router.routes)


@pytest.mark.parametrize("instrument_id", ["600519.SH", "000001.SZ", "430047.BJ"])
def test_real_exchange_qualified_instruments_pass_route_validation(
    monkeypatch: pytest.MonkeyPatch,
    instrument_id: str,
) -> None:
    """Valid nine-character Shanghai, Shenzhen, and Beijing identifiers reach the reader."""
    observed_instruments: list[str] = []
    observed_page_sizes: list[int] = []

    def read_page(**kwargs: object) -> dict[str, object]:
        observed_instruments.append(str(kwargs["instrument_id"]))
        observed_page_sizes.append(int(kwargs["page_size"]))
        return {
            "scope": {"instrument_id": str(kwargs["instrument_id"])},
            "records": [],
            "completeness": "unknown",
            "point_in_time_reconstruction": "unavailable",
            "period_amount_semantics": "unknown",
        }

    monkeypatch.setattr(archive_api, "_read_archive_page", read_page)
    app = FastAPI()
    app.include_router(archive_api.router, prefix="/api/v2/data")
    app.dependency_overrides[get_current_principal] = lambda: _principal(allowed=True)

    with TestClient(app) as client:
        response = client.get(
            "/api/v2/data/financial-statements/archive",
            params={
                "source_id": "ths_fuyao",
                "instrument_id": instrument_id,
                "statement_type": "income",
                "periodicity": "quarterly",
                "start_report_period": "2025-03-31",
                "end_report_period": "2025-03-31",
                "page_size": 199,
            },
        )

    assert response.status_code == 200
    assert response.json()["scope"]["instrument_id"] == instrument_id
    assert observed_instruments == [instrument_id]
    assert observed_page_sizes == [199]


@pytest.mark.parametrize(
    "extra_params",
    [
        [("cursor", "opaque")],
        [("as_of", "2025-04-25T00:00:00Z")],
        [("page_size", "100"), ("page_size", "101")],
    ],
    ids=["cursor", "as-of", "repeated-key"],
)
def test_route_rejects_unsupported_or_repeated_query_parameters(
    monkeypatch: pytest.MonkeyPatch,
    extra_params: list[tuple[str, str]],
) -> None:
    """The API does not silently ignore cursors, as_of values, or duplicate keys."""
    reader_calls = 0

    def unexpected_reader(**kwargs: object) -> dict[str, object]:
        nonlocal reader_calls
        reader_calls += 1
        pytest.fail("unsupported query parameters must fail before archive access")

    monkeypatch.setattr(archive_api, "_read_archive_page", unexpected_reader)
    app = FastAPI()
    app.include_router(archive_api.router, prefix="/api/v2/data")
    app.dependency_overrides[get_current_principal] = lambda: _principal(allowed=True)
    params = [
        ("source_id", "ths_fuyao"),
        ("instrument_id", "600519.SH"),
        ("statement_type", "income"),
        ("periodicity", "quarterly"),
        ("start_report_period", "2025-03-31"),
        ("end_report_period", "2025-03-31"),
        ("page_size", "100"),
        *extra_params,
    ]

    with TestClient(app) as client:
        response = client.get("/api/v2/data/financial-statements/archive", params=params)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unsupported_query"
    assert response.headers["cache-control"] == "private, no-store"
    assert reader_calls == 0


def test_source_id_with_trailing_newline_is_rejected_before_archive_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A final line break cannot exploit regex end-anchor semantics in source IDs."""
    reader_calls = 0

    def unexpected_reader(**kwargs: object) -> dict[str, object]:
        nonlocal reader_calls
        reader_calls += 1
        pytest.fail("invalid source ID must fail before archive access")

    monkeypatch.setattr(archive_api, "_read_archive_page", unexpected_reader)
    app = FastAPI()
    app.include_router(archive_api.router, prefix="/api/v2/data")
    app.dependency_overrides[get_current_principal] = lambda: _principal(allowed=True)

    with TestClient(app) as client:
        response = client.get(
            "/api/v2/data/financial-statements/archive",
            params={
                "source_id": "ths_fuyao\n",
                "instrument_id": "600519.SH",
                "statement_type": "income",
                "periodicity": "quarterly",
                "start_report_period": "2025-03-31",
                "end_report_period": "2025-03-31",
            },
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert reader_calls == 0


@pytest.mark.parametrize(
    ("start_report_period", "end_report_period"),
    [
        (date(2025, 4, 1), date(2025, 3, 31)),
        (date(2020, 1, 1), date(2030, 1, 9)),
    ],
)
def test_invalid_report_period_windows_are_rejected_before_archive_engine_creation(
    monkeypatch: pytest.MonkeyPatch,
    start_report_period: date,
    end_report_period: date,
) -> None:
    """Reversed or over-ten-year windows fail before the archive engine is touched."""

    def unexpected_engine() -> Engine:
        pytest.fail("an invalid report-period window must not create an archive engine")

    monkeypatch.setattr(archive_api, "_archive_engine_from_settings", unexpected_engine)
    query = _request_kwargs(_principal(allowed=True))
    query["start_report_period"] = start_report_period
    query["end_report_period"] = end_report_period

    with pytest.raises(HTTPException) as raised:
        asyncio.run(archive_api.query_financial_observation_archive(**query))

    assert raised.value.status_code == 422
    assert raised.value.detail["code"] == "invalid_request"


def test_page_size_above_internal_single_page_bound_is_rejected_by_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 200-row request is rejected at HTTP validation before SQL is scheduled."""
    reader_calls = 0

    def unexpected_reader(**kwargs: object) -> dict[str, object]:
        nonlocal reader_calls
        reader_calls += 1
        pytest.fail("page_size=200 must fail request validation")

    monkeypatch.setattr(archive_api, "_read_archive_page", unexpected_reader)
    app = FastAPI()
    app.include_router(archive_api.router, prefix="/api/v2/data")
    app.dependency_overrides[get_current_principal] = lambda: _principal(allowed=True)

    with TestClient(app) as client:
        response = client.get(
            "/api/v2/data/financial-statements/archive",
            params={
                "source_id": "ths_fuyao",
                "instrument_id": "600519.SH",
                "statement_type": "income",
                "periodicity": "quarterly",
                "start_report_period": "2025-03-31",
                "end_report_period": "2025-03-31",
                "page_size": 200,
            },
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert reader_calls == 0


def test_reader_has_more_rejects_the_entire_page() -> None:
    """A continuation requirement rejects the page before any records are exposed."""
    page = FinancialObservationArchivePage(rows=(_row(),), next_cursor=object())  # type: ignore[arg-type]

    with pytest.raises(archive_api.FinancialArchivePageTooLargeError):
        archive_api._response_payload(
            page,
            source_id="ths_fuyao",
            instrument_id="600519.SH",
            statement_type="income",
            periodicity="quarterly",
            start_report_period=date(2025, 3, 31),
            end_report_period=date(2025, 3, 31),
        )


def test_route_maps_oversized_page_to_413_without_a_partial_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The HTTP error for an oversized page contains no records field."""

    def page_too_large(**kwargs: object) -> dict[str, object]:
        raise archive_api.FinancialArchivePageTooLargeError

    monkeypatch.setattr(archive_api, "_read_archive_page", page_too_large)
    with pytest.raises(HTTPException) as raised:
        asyncio.run(
            archive_api.query_financial_observation_archive(
                **_request_kwargs(_principal(allowed=True))
            )
        )

    assert raised.value.status_code == 413
    assert raised.value.detail == {
        "code": "response_too_large",
        "message": "Financial archive result exceeds the 256 KiB response limit",
    }


def test_public_projection_preserves_decimal_null_and_three_distinct_dates() -> None:
    """The allowlist keeps exact decimal text, NULL, report dates, and UTC observation time."""
    row = _row(value=None)
    record = archive_api._row_payload(row)

    assert record == {
        "source_id": "ths_fuyao",
        "instrument_id": "600519.SH",
        "statement_type": "income",
        "periodicity": "quarterly",
        "report_period": "2025-03-31",
        "source_disclosure_date": "2025-04-25",
        "observed_at_utc": "2025-04-25T03:04:05.006000Z",
        "item_code": "revenue",
        "value_decimal_text": None,
        "native_unit": "CNY",
    }
    assert _row().value_decimal_text == "123.4500"
    assert "source_metadata_json" not in record


def test_success_uses_v2_private_response_headers_and_raw_success_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The success response follows the v2 data shape and cache policy."""
    payload = {
        "scope": {"instrument_id": "600519.SH"},
        "records": [],
        "completeness": "unknown",
        "point_in_time_reconstruction": "unavailable",
        "period_amount_semantics": "unknown",
    }

    monkeypatch.setattr(archive_api, "_read_archive_page", lambda **kwargs: payload)
    response = asyncio.run(
        archive_api.query_financial_observation_archive(**_request_kwargs(_principal(allowed=True)))
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["vary"] == "Authorization, X-API-Key"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert b'"scope"' in response.body
    assert b'"success"' not in response.body
    assert b'"data"' not in response.body


def test_error_uses_v2_envelope_and_private_cache_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Archive failures are sanitized by the existing v2 route wrapper."""

    def unavailable(**kwargs: object) -> dict[str, object]:
        raise archive_api.FinancialArchiveUnavailableError(
            "mysql+pymysql://user:secret@private-host/financial_archive"
        )

    monkeypatch.setattr(archive_api, "_read_archive_page", unavailable)
    app = FastAPI()
    app.include_router(archive_api.router, prefix="/api/v2/data")
    app.dependency_overrides[get_current_principal] = lambda: _principal(allowed=True)

    with TestClient(app) as client:
        response = client.get(
            "/api/v2/data/financial-statements/archive",
            params={
                "source_id": "ths_fuyao",
                "instrument_id": "600519.SH",
                "statement_type": "income",
                "periodicity": "quarterly",
                "start_report_period": "2025-03-31",
                "end_report_period": "2025-03-31",
            },
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "published_data_unavailable"
    assert response.headers["cache-control"] == "private, no-store"
    assert "secret" not in response.text
    assert "private-host" not in response.text


def test_full_serialized_response_has_a_hard_byte_limit() -> None:
    """The endpoint rejects a response whose complete JSON wrapper exceeds its byte cap."""
    page = FinancialObservationArchivePage(
        rows=(_row(item_code="x" * (archive_api._MAX_RESPONSE_BYTES + 1)),),
        next_cursor=None,
    )

    with pytest.raises(archive_api.FinancialArchivePageTooLargeError):
        archive_api._response_payload(
            page,
            source_id="ths_fuyao",
            instrument_id="600519.SH",
            statement_type="income",
            periodicity="quarterly",
            start_report_period=date(2025, 3, 31),
            end_report_period=date(2025, 3, 31),
        )


def test_full_serialized_response_accepts_exact_byte_limit() -> None:
    """A body exactly at the documented byte cap is retained as a complete page."""
    common_scope = {
        "source_id": "ths_fuyao",
        "instrument_id": "600519.SH",
        "statement_type": "income",
        "periodicity": "quarterly",
        "start_report_period": date(2025, 3, 31),
        "end_report_period": date(2025, 3, 31),
    }
    base_page = FinancialObservationArchivePage(
        rows=(_row(item_code=""),),
        next_cursor=None,
    )
    base_payload = archive_api._response_payload(base_page, **common_scope)
    base_size = len(JSONResponse(content=base_payload).body)
    exact_page = FinancialObservationArchivePage(
        rows=(_row(item_code="x" * (archive_api._MAX_RESPONSE_BYTES - base_size)),),
        next_cursor=None,
    )

    payload = archive_api._response_payload(exact_page, **common_scope)

    assert len(JSONResponse(content=payload).body) == archive_api._MAX_RESPONSE_BYTES
