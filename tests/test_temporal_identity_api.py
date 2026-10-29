"""Synthetic ASGI contract tests for the pinned temporal identity API."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Any

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import Engine, create_engine, event

from opendata.api.dependencies import Principal, get_current_principal
from opendata.api.published_access import DatasetGrant, DatasetGrantRegistry
from opendata.api.published_data import get_dataset_grants
from opendata.api.temporal_identity import (
    MAX_REQUEST_BYTES,
    MAX_RESOLUTION_SEGMENTS,
    TemporalIdentityPin,
    TemporalIdentityPinRegistry,
    router,
)
from opendata.data.temporal_identity_store import TemporalIdentitySnapshotStore
from opendata.data.temporal_instrument_identity import (
    TemporalIdentityAssignment,
    TemporalIdentityRegistry,
)
from opendata.data.temporal_instrument_identity_snapshot import (
    encode_temporal_identity_snapshot,
)
from opendata.models.user import User, UserRole

if TYPE_CHECKING:
    from pathlib import Path

_UTC = timezone.utc
_DATASET_ID = "instrument.identity"
_SOURCE_ID = "akshare"
_AUTHORITY_ID = "reviewed-market-authority"
_REGISTRY_ID = "cn-a-share"
_REFERENCE_VERSION = "identity-reference-v1"
_SNAPSHOT_SHA256 = "a" * 64
_PATH = "/api/v2/identity/resolve"
_CREATE_TEMPORAL_SNAPSHOT_HEADER = """
CREATE TABLE od_temporal_identity_snapshots (
    authority_id VARCHAR(128) COLLATE BINARY NOT NULL,
    registry_id VARCHAR(128) COLLATE BINARY NOT NULL,
    reference_version VARCHAR(128) COLLATE BINARY NOT NULL,
    total_bytes BIGINT NOT NULL CHECK (total_bytes BETWEEN 1 AND 67108864),
    chunk_count SMALLINT NOT NULL CHECK (chunk_count BETWEEN 1 AND 256),
    snapshot_sha256 BLOB NOT NULL
      CHECK (typeof(snapshot_sha256) = 'blob' AND length(snapshot_sha256) = 32),
    created_at_us BIGINT NOT NULL CHECK (created_at_us >= 0),
    CHECK (length(authority_id) BETWEEN 1 AND 128
      AND length(CAST(authority_id AS BLOB)) = length(authority_id)),
    CHECK (length(registry_id) BETWEEN 1 AND 128
      AND length(CAST(registry_id AS BLOB)) = length(registry_id)),
    CHECK (length(reference_version) BETWEEN 1 AND 128
      AND length(CAST(reference_version AS BLOB)) = length(reference_version)),
    PRIMARY KEY (authority_id, registry_id, reference_version)
)
"""
_CREATE_TEMPORAL_SNAPSHOT_CHUNKS = """
CREATE TABLE od_temporal_identity_snapshot_chunks (
    authority_id VARCHAR(128) COLLATE BINARY NOT NULL,
    registry_id VARCHAR(128) COLLATE BINARY NOT NULL,
    reference_version VARCHAR(128) COLLATE BINARY NOT NULL,
    chunk_no SMALLINT NOT NULL CHECK (chunk_no BETWEEN 0 AND 255),
    chunk_bytes BLOB NOT NULL
      CHECK (typeof(chunk_bytes) = 'blob' AND length(chunk_bytes) BETWEEN 1 AND 262144),
    chunk_sha256 BLOB NOT NULL
      CHECK (typeof(chunk_sha256) = 'blob' AND length(chunk_sha256) = 32),
    CHECK (length(authority_id) BETWEEN 1 AND 128
      AND length(CAST(authority_id AS BLOB)) = length(authority_id)),
    CHECK (length(registry_id) BETWEEN 1 AND 128
      AND length(CAST(registry_id AS BLOB)) = length(registry_id)),
    CHECK (length(reference_version) BETWEEN 1 AND 128
      AND length(CAST(reference_version AS BLOB)) = length(reference_version)),
    PRIMARY KEY (authority_id, registry_id, reference_version, chunk_no),
    FOREIGN KEY (authority_id, registry_id, reference_version)
      REFERENCES od_temporal_identity_snapshots
        (authority_id, registry_id, reference_version)
      ON DELETE RESTRICT
)
"""


class _SyntheticStore:
    """Record exact read pins while returning only a synthetic immutable registry."""

    def __init__(self, registry: TemporalIdentityRegistry, error: Exception | None = None) -> None:
        self.registry = registry
        self.error = error
        self.calls: list[dict[str, str]] = []

    def get(
        self,
        *,
        authority_id: str,
        registry_id: str,
        reference_version: str,
        expected_sha256: str,
    ) -> TemporalIdentityRegistry:
        """Capture the store key and return a test registry without database access."""
        self.calls.append(
            {
                "authority_id": authority_id,
                "registry_id": registry_id,
                "reference_version": reference_version,
                "expected_sha256": expected_sha256,
            }
        )
        if self.error is not None:
            raise self.error
        return self.registry


def _registry(
    *, source_id: str = _SOURCE_ID, assignment_count: int = 1
) -> TemporalIdentityRegistry:
    """Build a bounded synthetic registry with known rows and unknown gaps."""
    assignments = tuple(
        TemporalIdentityAssignment(
            assignment_id=f"assignment-{index}",
            instrument_id="instrument-600000",
            exchange="XSHG",
            symbol="600000",
            valid_from=date(2020, 1, 1).fromordinal(date(2020, 1, 1).toordinal() + index * 2),
            valid_to=date(2020, 1, 1).fromordinal(date(2020, 1, 1).toordinal() + index * 2 + 1),
            source_id=source_id,
            source_version="fixture-source-v1",
            evidence_ref="docs/evidence/temporal/fixture.json",
            source_sha256="b" * 64,
        )
        for index in range(assignment_count)
    )
    return TemporalIdentityRegistry(
        reference_version=_REFERENCE_VERSION,
        evidence_ref="docs/evidence/temporal/registry-fixture.json",
        assignments=assignments,
    )


def _grant(*, source_id: str = _SOURCE_ID) -> DatasetGrantRegistry:
    """Return a narrow research grant for one identity dataset and source."""
    return DatasetGrantRegistry(
        (
            DatasetGrant(
                dataset_id=_DATASET_ID,
                domain="equity",
                source_id=source_id,
                permitted_operations=("identity.resolve",),
                purposes=("research",),
                valid_from=datetime(2000, 1, 1, tzinfo=_UTC),
                valid_until=datetime(2100, 1, 1, tzinfo=_UTC),
                review_evidence="OD204-TEST",
            ),
        )
    )


def _pin_registry(
    *,
    registry_id: str = _REGISTRY_ID,
    reference_version: str = _REFERENCE_VERSION,
    snapshot_sha256: str = _SNAPSHOT_SHA256,
) -> TemporalIdentityPinRegistry:
    """Return one server-controlled pin for the synthetic dataset."""
    return TemporalIdentityPinRegistry(
        (
            TemporalIdentityPin(
                dataset_id=_DATASET_ID,
                source_id=_SOURCE_ID,
                authority_id=_AUTHORITY_ID,
                registry_id=registry_id,
                reference_version=reference_version,
                snapshot_sha256=snapshot_sha256,
            ),
        )
    )


def _principal(*, scopes: tuple[str, ...] | None = ("identity.resolve", "equity")) -> Principal:
    """Create an active synthetic user, optionally behind an API-key scope set."""
    user = User(
        id=41,
        username="identity-api-test",
        email="identity-api@example.test",
        hashed_password="not-a-real-password-hash",
        role=UserRole.USER,
        is_active=True,
    )
    if scopes is None:
        return Principal(user=user)
    return Principal(user=user, api_key_id=91, scopes=scopes)


def _admin_jwt_principal() -> Principal:
    """Create an admin JWT principal under the existing administrator policy."""
    user = User(
        id=42,
        username="identity-api-admin",
        email="identity-api-admin@example.test",
        hashed_password="not-a-real-password-hash",
        role=UserRole.ADMIN,
        is_active=True,
    )
    return Principal(user=user)


def _app(
    *,
    store: object | None = None,
    principal: Principal | None = None,
    grants: DatasetGrantRegistry | None = None,
    include_pins: bool = True,
) -> FastAPI:
    """Mount the API with injected synthetic dependencies and no app lifespan."""
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_principal] = lambda: principal or _principal()
    app.dependency_overrides[get_dataset_grants] = lambda: grants or _grant()
    if include_pins:
        app.state.temporal_identity_pin_registry = _pin_registry()
    if store is not None:
        app.state.temporal_identity_snapshot_store = store
    return app


def _temporary_sqlite_store(
    tmp_path: Path, registry: TemporalIdentityRegistry
) -> tuple[Engine, TemporalIdentitySnapshotStore, str]:
    """Create and seed a snapshot store backed only by a test-owned SQLite file."""
    database_path = tmp_path / "temporal-identity-snapshot.sqlite"
    engine = create_engine(f"sqlite:///{database_path}")

    try:

        @event.listens_for(engine, "connect")
        def _enable_foreign_keys(connection: object, _record: object) -> None:
            cursor = connection.cursor()  # type: ignore[attr-defined]
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        with engine.begin() as connection:
            connection.exec_driver_sql(_CREATE_TEMPORAL_SNAPSHOT_HEADER)
            connection.exec_driver_sql(_CREATE_TEMPORAL_SNAPSHOT_CHUNKS)

        encoded = encode_temporal_identity_snapshot(registry)
        digest = hashlib.sha256(encoded).hexdigest()
        store = TemporalIdentitySnapshotStore(engine)
        store.put(
            authority_id=_AUTHORITY_ID,
            registry_id=_REGISTRY_ID,
            registry=registry,
            expected_sha256=digest,
        )
        return engine, store, digest
    except BaseException:
        engine.dispose()
        raise


def _payload(**changes: object) -> dict[str, object]:
    """Build the client echo of a server pin and one explicit pair selector."""
    payload: dict[str, object] = {
        "dataset_id": _DATASET_ID,
        "authority_id": _AUTHORITY_ID,
        "registry_id": _REGISTRY_ID,
        "reference_version": _REFERENCE_VERSION,
        "snapshot_sha256": _SNAPSHOT_SHA256,
        "window_start": "2020-01-01",
        "window_end_exclusive": "2020-01-05",
        "exchange": "XSHG",
        "symbol": "600000",
    }
    payload.update(changes)
    return payload


async def _request(
    app: FastAPI,
    *,
    content: bytes | None = None,
    json: Any = None,
    **kwargs: Any,
) -> Response:
    """Send a bounded in-process ASGI request without opening sockets."""
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        return await client.post(_PATH, content=content, json=json, **kwargs)


@pytest.mark.asyncio
async def test_pinned_pair_resolution_preserves_unknown_segment_and_uses_server_pin() -> None:
    """Return known and unknown intervals while reading under trusted server pin values."""
    store = _SyntheticStore(_registry())
    response = await _request(_app(store=store), json=_payload())

    assert response.status_code == 200
    body = response.json()
    assert body["subject"] == "pair"
    assert [segment["status"] for segment in body["segments"]] == ["known", "unknown"]
    assert body["segments"][1]["start"] == "2020-01-02"
    assert body["segments"][1]["end_exclusive"] == "2020-01-05"
    assert body["completeness"] == "not_established_by_identity_resolution"
    assert body["source_integrity"] == "snapshot_bytes_pinned_source_evidence_not_authenticated"
    assert "evidence_ref" not in body["segments"][0]
    assert "source_sha256" not in body["segments"][0]
    assert store.calls == [
        {
            "authority_id": _AUTHORITY_ID,
            "registry_id": _REGISTRY_ID,
            "reference_version": _REFERENCE_VERSION,
            "expected_sha256": _SNAPSHOT_SHA256,
        }
    ]


@pytest.mark.asyncio
async def test_route_reads_only_the_exact_server_pinned_sqlite_snapshot(tmp_path: Path) -> None:
    """Exercise API lookup, digest verification, and unknown keys on owned SQLite."""
    registry = _registry()
    engine, store, digest = _temporary_sqlite_store(tmp_path, registry)
    database_path = tmp_path / "temporal-identity-snapshot.sqlite"
    assert engine.url.database == str(database_path)

    try:
        app = _app(store=store)
        app.state.temporal_identity_pin_registry = _pin_registry(snapshot_sha256=digest)
        resolved = await _request(
            app,
            json=_payload(snapshot_sha256=digest),
        )

        assert resolved.status_code == 200
        assert resolved.json()["snapshot_sha256"] == digest
        assert resolved.json()["segments"][0]["instrument_id"] == "instrument-600000"
        assert database_path.exists()

        mismatched_pin_app = _app(store=store)
        mismatched_digest = "c" * 64
        mismatched_pin_app.state.temporal_identity_pin_registry = _pin_registry(
            snapshot_sha256=mismatched_digest
        )
        mismatched = await _request(
            mismatched_pin_app,
            json=_payload(snapshot_sha256=mismatched_digest),
        )
        assert mismatched.status_code == 503
        assert mismatched.json()["error"]["code"] == "temporal_identity_unavailable"
        assert mismatched_digest not in mismatched.text

        unknown_pin_app = _app(store=store)
        unknown_registry_id = "reviewed-registry-not-in-store"
        unknown_pin_app.state.temporal_identity_pin_registry = _pin_registry(
            registry_id=unknown_registry_id,
            snapshot_sha256=digest,
        )
        unknown = await _request(
            unknown_pin_app,
            json=_payload(registry_id=unknown_registry_id, snapshot_sha256=digest),
        )
        assert unknown.status_code == 503
        assert unknown.json()["error"]["code"] == "temporal_identity_unavailable"
        assert unknown_registry_id not in unknown.text
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_instrument_resolution_accepts_only_opaque_instrument_selector() -> None:
    """Resolve an instrument selector and report its pair without inventing coverage."""
    store = _SyntheticStore(_registry())
    payload = _payload(exchange=None, symbol=None, instrument_id="instrument-600000")
    response = await _request(_app(store=store), json=payload)

    assert response.status_code == 200
    body = response.json()
    assert body["subject"] == "instrument"
    assert body["pair"] is None
    assert body["segments"][0]["pair"] == {"exchange": "XSHG", "symbol": "600000"}


@pytest.mark.asyncio
async def test_admin_jwt_uses_existing_admin_permission_without_api_key_scopes() -> None:
    """Keep the existing administrator JWT policy while regular JWT stays denied."""
    store = _SyntheticStore(_registry())
    response = await _request(
        _app(store=store, principal=_admin_jwt_principal()),
        json=_payload(),
    )

    assert response.status_code == 200
    assert len(store.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("principal", "grants"),
    [
        (_principal(scopes=("equity",)), _grant()),
        (_principal(scopes=("identity.resolve", "other-domain")), _grant()),
        (_principal(scopes=None), _grant()),
        (_principal(), _grant(source_id="another-source")),
    ],
)
async def test_authorization_denials_happen_before_temporal_store(
    principal: Principal,
    grants: DatasetGrantRegistry,
) -> None:
    """Require exact operation/domain/source grants before invoking the store."""
    store = _SyntheticStore(_registry())
    response = await _request(
        _app(store=store, principal=principal, grants=grants),
        json=_payload(),
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "dataset_unavailable"
    assert store.calls == []


@pytest.mark.asyncio
async def test_client_pin_mismatch_is_denied_before_store() -> None:
    """Refuse a caller-supplied alternate snapshot without probing the store."""
    store = _SyntheticStore(_registry())
    response = await _request(
        _app(store=store),
        json=_payload(reference_version="untrusted-latest"),
    )

    assert response.status_code == 403
    assert store.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("snapshot_sha256", "c" * 64),
        ("authority_id", "untrusted-authority"),
        ("registry_id", "untrusted-registry"),
    ],
)
async def test_client_digest_authority_and_registry_mismatches_never_read_store(
    field: str,
    value: str,
) -> None:
    """Reject each caller-selected pin dimension before any snapshot-store call."""
    store = _SyntheticStore(_registry())
    response = await _request(_app(store=store), json=_payload(**{field: value}))

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "dataset_unavailable"
    assert store.calls == []


@pytest.mark.asyncio
async def test_missing_pin_or_store_fails_closed_without_store_access() -> None:
    """Leave the endpoint disabled unless both trusted pin and read store are injected."""
    store = _SyntheticStore(_registry())
    no_pin = await _request(_app(store=store, include_pins=False), json=_payload())
    no_store = await _request(_app(), json=_payload())

    assert no_pin.status_code == 503
    assert no_store.status_code == 503
    assert store.calls == []


@pytest.mark.asyncio
async def test_mixed_source_registry_fails_closed_and_never_returns_evidence_text() -> None:
    """Reject a snapshot crossing the granted source and keep free-form evidence private."""
    registry = _registry(source_id="ungranted-source")
    store = _SyntheticStore(registry)
    response = await _request(_app(store=store), json=_payload())

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "temporal_identity_unavailable"
    assert len(store.calls) == 1
    assert "docs/evidence" not in response.text
    assert "ungranted-source" not in response.text


@pytest.mark.asyncio
async def test_store_exception_is_sanitized() -> None:
    """Do not return a driver's DSN or exception string to API consumers."""
    store = _SyntheticStore(_registry(), RuntimeError("mysql://user:password@host/private"))
    response = await _request(_app(store=store), json=_payload())

    assert response.status_code == 503
    assert "mysql://" not in response.text
    assert "password" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"instrument_id": "instrument-600000"},
        {"exchange": None, "symbol": None},
        {"exchange": "XSHG", "symbol": "600000", "instrument_id": "instrument-600000"},
        {"exchange": "SH", "symbol": "600000"},
        {"symbol": "６０００００"},
        {"window_start": "2020-01-05", "window_end_exclusive": "2020-01-01"},
        {"window_start": "2000-01-01", "window_end_exclusive": "2020-01-01"},
    ],
)
async def test_invalid_selector_or_window_is_rejected_before_store(
    changes: dict[str, object],
) -> None:
    """Reject malformed selector families, exchange labels, symbols, and windows."""
    store = _SyntheticStore(_registry())
    response = await _request(_app(store=store), json=_payload(**changes))

    assert response.status_code == 422
    assert store.calls == []


@pytest.mark.asyncio
async def test_body_size_duplicate_keys_and_query_string_are_bounded() -> None:
    """Reject oversize, duplicate-key, and query-string forms before store access."""
    store = _SyntheticStore(_registry())
    app = _app(store=store)
    oversized = await _request(
        app,
        content=b" " * (MAX_REQUEST_BYTES + 1),
        headers={"Content-Type": "application/json"},
    )
    duplicate = await _request(
        app,
        content=b'{"dataset_id":"instrument.identity","dataset_id":"other"}',
        headers={"Content-Type": "application/json"},
    )
    query = await _request(app, json=_payload(), params={"latest": "true"})

    assert oversized.status_code == 413
    assert oversized.json()["error"]["code"] == "request_too_large"
    assert duplicate.status_code == 422
    assert query.status_code == 422
    assert store.calls == []


@pytest.mark.asyncio
async def test_unrecognized_http_exception_detail_is_not_returned() -> None:
    """Map arbitrary dependency details to a fixed public error contract."""
    store = _SyntheticStore(_registry())
    app = _app(store=store)

    def reject_with_untrusted_detail() -> Principal:
        raise HTTPException(
            status_code=413,
            detail={"code": "attacker_code", "message": "private diagnostic detail"},
        )

    app.dependency_overrides[get_current_principal] = reject_with_untrusted_detail
    response = await _request(app, json=_payload())

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"
    assert "attacker_code" not in response.text
    assert "private diagnostic detail" not in response.text
    assert store.calls == []


@pytest.mark.asyncio
async def test_huge_decimal_content_length_is_rejected_without_integer_conversion() -> None:
    """Reject a five-thousand-digit length as oversized without reaching the store."""
    store = _SyntheticStore(_registry())
    response = await _request(
        _app(store=store),
        content=json.dumps(_payload()).encode(),
        headers={
            "Content-Type": "application/json",
            "Content-Length": "9" * 5_000,
        },
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"
    assert store.calls == []


@pytest.mark.asyncio
async def test_zero_padded_content_length_accepts_exact_body_limit() -> None:
    """Compare a long zero-padded decimal safely and accept the exact byte limit."""
    store = _SyntheticStore(_registry())
    payload = json.dumps(_payload(), separators=(",", ":")).encode()
    body = payload + (b" " * (MAX_REQUEST_BYTES - len(payload)))
    response = await _request(
        _app(store=store),
        content=body,
        headers={
            "Content-Type": "application/json",
            "Content-Length": ("0" * 5_000) + str(MAX_REQUEST_BYTES),
        },
    )

    assert len(body) == MAX_REQUEST_BYTES
    assert response.status_code == 200
    assert len(store.calls) == 1


@pytest.mark.asyncio
async def test_zero_padded_content_length_over_limit_is_rejected() -> None:
    """Reject a zero-padded decimal just above the body limit before store access."""
    store = _SyntheticStore(_registry())
    response = await _request(
        _app(store=store),
        content=json.dumps(_payload()).encode(),
        headers={
            "Content-Type": "application/json",
            "Content-Length": ("0" * 5_000) + str(MAX_REQUEST_BYTES + 1),
        },
    )

    assert response.status_code == 413
    assert store.calls == []


@pytest.mark.asyncio
async def test_deep_json_is_rejected_before_store_access() -> None:
    """Reject nested JSON using a bounded scan before general JSON decoding."""
    store = _SyntheticStore(_registry())
    nested = b'{"unexpected":' + (b"[" * 64) + b"0" + (b"]" * 64) + b"}"
    response = await _request(
        _app(store=store),
        content=nested,
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert store.calls == []


@pytest.mark.asyncio
async def test_more_than_ten_top_level_json_fields_are_rejected() -> None:
    """Bound selector object complexity independently of its encoded byte size."""
    store = _SyntheticStore(_registry())
    payload = {**_payload(), "extra_one": "x", "extra_two": "x"}
    response = await _request(_app(store=store), json=payload)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert store.calls == []


@pytest.mark.asyncio
async def test_oversized_json_is_rejected_before_store_access() -> None:
    """Enforce the eight-KiB request bound for an otherwise valid JSON object."""
    store = _SyntheticStore(_registry())
    oversized = b'{"unexpected":"' + (b"a" * MAX_REQUEST_BYTES) + b'"}'
    response = await _request(
        _app(store=store),
        content=oversized,
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"
    assert store.calls == []


@pytest.mark.asyncio
async def test_segment_limit_is_enforced() -> None:
    """Refuse a resolution whose known rows and gaps exceed the API segment cap."""
    registry = _registry(assignment_count=(MAX_RESOLUTION_SEGMENTS // 2) + 2)
    store = _SyntheticStore(registry)
    end = date(2020, 1, 1).fromordinal(
        date(2020, 1, 1).toordinal() + (MAX_RESOLUTION_SEGMENTS // 2 + 2) * 2
    )
    response = await _request(
        _app(store=store),
        json=_payload(window_end_exclusive=end.isoformat()),
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "identity_resolution_too_large"
    assert len(store.calls) == 1
