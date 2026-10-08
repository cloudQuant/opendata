"""Behavior tests for authenticated local minute-data queries."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import create_engine, inspect, select

from opendata.data.minute_archive import get_minute_archive_engine, ingest_minute_shard
from opendata.main import app
from opendata.models.minute_archive import MinuteArchiveShard
from opendata.services.api_key_service import ApiKeyService

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy import Engine


@pytest.fixture
def api_minute_archive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[tuple[Engine, Path]]:
    """Inject a test-only mainDB engine and isolated Parquet root."""
    import opendata.api.minute_data as minute_api

    engine = create_engine(f"sqlite:///{tmp_path / 'main-api.db'}")
    MinuteArchiveShard.__table__.create(engine)
    root = tmp_path / "minute_archive"
    monkeypatch.setattr(minute_api, "get_minute_archive_root", lambda: root)
    app.dependency_overrides[get_minute_archive_engine] = lambda: engine
    yield engine, root
    app.dependency_overrides.pop(get_minute_archive_engine, None)
    engine.dispose()


def _record(symbol: str, timestamp: str, close: float = 10.0) -> dict[str, object]:
    """Return one normalized row for an isolated archive fixture."""
    return {
        "symbol": symbol,
        "timestamp": timestamp,
        "open": close - 1,
        "high": close + 1,
        "low": close - 2,
        "close": close,
        "volume": 100,
        "amount": 1000,
    }


def _params(*, symbols: list[str], **extra: str | int) -> list[tuple[str, str]]:
    """Build repeated query parameters in an HTTPX-compatible form."""
    values: list[tuple[str, str]] = [("symbols", symbol) for symbol in symbols]
    values.extend((key, str(value)) for key, value in extra.items())
    return values


def test_openapi_publishes_minute_file_query() -> None:
    """The API advertises its three-segment file route separately from daily data."""
    assert "/api/v1/data/minute/{asset_class}/{domain}" in app.openapi()["paths"]


async def test_minute_query_requires_authentication(
    test_client,
    api_minute_archive: tuple[Engine, Path],
) -> None:
    """Unauthenticated callers cannot inspect minute archive contents."""
    response = await test_client.get(
        "/api/v1/data/minute/equity/stock_daily",
        params=_params(symbols=["AAPL"], source="sample-feed", period="1m"),
    )

    assert response.status_code == 401


async def test_minute_query_reads_only_main_index_and_local_parquet(
    test_client,
    test_user_token: str,
    api_minute_archive: tuple[Engine, Path],
) -> None:
    """Authenticated data is returned from the isolated main index and file root."""
    engine, root = api_minute_archive
    ingest_minute_shard(
        engine,
        root,
        domain="stock_daily",
        symbol="AAPL",
        source="sample-feed",
        period="1m",
        records=[
            _record("AAPL", "2026-06-01T09:30:00Z"),
            _record("AAPL", "2026-06-01T09:31:00Z", close=11),
        ],
    )
    response = await test_client.get(
        "/api/v1/data/minute/equity/stock_daily",
        params=_params(
            symbols=["AAPL"],
            source="sample-feed",
            period="1m",
            start="2026-06-01",
            end="2026-06-01",
            fields="close",
            page=2,
            page_size=1,
        ),
        headers={"Authorization": f"Bearer {test_user_token}"},
    )

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["layer"] == "file"
    assert payload["count"] == 2
    assert payload["page"] == 2
    assert payload["columns"] == ["symbol", "timestamp", "period", "source", "close"]
    assert payload["rows"] == [
        {
            "symbol": "AAPL",
            "timestamp": "2026-06-01T09:31:00+00:00",
            "period": "1m",
            "source": "sample-feed",
            "close": 11.0,
        }
    ]
    assert inspect(engine).get_table_names() == ["minute_archive_shards"]


async def test_api_key_domain_scope_is_enforced(
    test_client,
    test_db,
    test_user_token: str,
    api_minute_archive: tuple[Engine, Path],
) -> None:
    """A key scoped to another domain receives the existing 403 policy."""
    from opendata.models.user import User

    user = await test_db.scalar(select(User).where(User.email == "testuser@example.com"))
    assert user is not None
    issued = await ApiKeyService(test_db).issue(
        owner=user, name="minute-scope", scopes=["index_daily"]
    )
    response = await test_client.get(
        "/api/v1/data/minute/equity/stock_daily",
        params=_params(symbols=["AAPL"], source="sample-feed", period="1m"),
        headers={"X-API-Key": issued.plaintext},
    )

    assert response.status_code == 403


async def test_unsupported_minute_domain_returns_not_found(
    test_client,
    test_user_token: str,
    api_minute_archive: tuple[Engine, Path],
) -> None:
    """Wrong asset-class/domain pairs do not fall through to daily warehouse routes."""
    response = await test_client.get(
        "/api/v1/data/minute/futures/stock_daily",
        params=_params(symbols=["AAPL"], source="sample-feed", period="1m"),
        headers={"Authorization": f"Bearer {test_user_token}"},
    )

    assert response.status_code == 404


async def test_maximum_calendar_end_returns_bad_request(
    test_client,
    test_user_token: str,
    api_minute_archive: tuple[Engine, Path],
) -> None:
    """An unrepresentable exclusive upper bound is rejected as a filter error."""
    response = await test_client.get(
        "/api/v1/data/minute/equity/stock_daily",
        params=_params(
            symbols=["AAPL"],
            source="sample-feed",
            period="1m",
            start="9999-12-31",
            end="9999-12-31",
        ),
        headers={"Authorization": f"Bearer {test_user_token}"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "end date is outside the supported query range"


async def test_corrupt_minute_file_returns_server_error_instead_of_empty_success(
    test_client,
    test_user_token: str,
    api_minute_archive: tuple[Engine, Path],
) -> None:
    """A missing indexed file is surfaced as an integrity failure."""
    engine, root = api_minute_archive
    shard = ingest_minute_shard(
        engine,
        root,
        domain="stock_daily",
        symbol="AAPL",
        source="sample-feed",
        period="1m",
        records=[_record("AAPL", "2026-06-01T09:30:00Z")],
    )
    (root / shard.relative_path).unlink()
    response = await test_client.get(
        "/api/v1/data/minute/equity/stock_daily",
        params=_params(
            symbols=["AAPL"],
            source="sample-feed",
            period="1m",
            start="2026-06-01",
            end="2026-06-01",
        ),
        headers={"Authorization": f"Bearer {test_user_token}"},
    )

    assert response.status_code == 500
    assert response.json()["detail"] == "minute archive integrity check failed"
