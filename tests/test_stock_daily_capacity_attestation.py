"""Offline tests for the digest-pinned stock-daily capacity gate."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from datetime import date
from typing import TYPE_CHECKING

import pytest
from fastapi import HTTPException

from opendata.api import stock_daily_capacity as capacity_module
from opendata.api.stock_daily_capacity import (
    MAX_CAPACITY_ATTESTATION_BYTES,
    StockDailyCapacityAttestation,
    StockDailyCapacityError,
    get_stock_daily_capacity_gate,
    load_stock_daily_capacity_attestation,
)
from opendata.api.stock_daily_publication import (
    DATASET_ID,
    _require_capacity_attestation,
)
from opendata.data.dataset_contract_registry import canonical_sha256
from tests import test_stock_daily_publication_api as api_fixtures

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from fastapi import FastAPI

    from opendata.data.platform_contracts import DatasetContract
    from opendata.pipeline.publication_store import PublicationStore

pytest_plugins = ("tests.test_stock_daily_publication_api",)

START = api_fixtures.START
END_EXCLUSIVE = api_fixtures.END_EXCLUSIVE
PIN_MISMATCH = "a" * 32
_ENV_FLAG = "STOCK_DAILY_PUBLICATION_API_ENABLED"
_ENV_FILE = "STOCK_DAILY_PUBLICATION_CAPACITY_FILE"
_ENV_SHA = "STOCK_DAILY_PUBLICATION_CAPACITY_SHA256"


def _document(
    contract: DatasetContract,
    publication_id: str | None,
    **overrides: object,
) -> dict[str, object]:
    """Return one otherwise-valid attestation document for loader tests."""
    document: dict[str, object] = {
        "schema_version": "stock-daily-capacity-v1",
        "dataset_id": DATASET_ID,
        "source_id": "akshare.daily",
        "semantic_version": "stock-daily-raw-v1",
        "contract_sha256": canonical_sha256(contract),
        "publication_id": publication_id,
        "max_window_days": 3_660,
        "max_page_size": 1_000,
        "not_before": "2020-01-01T00:00:00Z",
        "expires_at": "2100-01-01T00:00:00Z",
        "evidence_ref": "tests/test_stock_daily_capacity_attestation.py",
        "attested_by": "offline-test",
    }
    document.update(overrides)
    return document


def _write_bytes(path: Path, content: bytes) -> str:
    """Write exact bytes and return their lowercase SHA-256 digest."""
    path.write_bytes(content)
    return hashlib.sha256(content).hexdigest()


def _write_document(path: Path, document: Mapping[str, object]) -> str:
    """Serialize one document deterministically and return its file digest."""
    return _write_bytes(
        path,
        json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
    )


def _use_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep gate tests hermetic when a developer `.env` exists in the repository."""
    monkeypatch.setattr(capacity_module, "_server_setting", os.environ.get)


def _enable(
    monkeypatch: pytest.MonkeyPatch,
    path: Path,
    digest: str,
    *,
    flag: str = "true",
) -> None:
    """Configure the three server settings for one hermetic request."""
    _use_process_environment(monkeypatch)
    monkeypatch.setenv(_ENV_FLAG, flag)
    monkeypatch.setenv(_ENV_FILE, str(path))
    monkeypatch.setenv(_ENV_SHA, digest)


def _disable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every gate setting so the default-off path is exercised."""
    _use_process_environment(monkeypatch)
    for name in (_ENV_FLAG, _ENV_FILE, _ENV_SHA):
        monkeypatch.delenv(name, raising=False)


def _enable_gate_in_app(
    app: FastAPI,
    attestation: StockDailyCapacityAttestation,
) -> None:
    """Override the gate dependency with one explicit synthetic attestation."""
    app.dependency_overrides[get_stock_daily_capacity_gate] = lambda: attestation


@pytest.mark.asyncio
async def test_default_off_returns_404_without_publication_access(
    stock_daily_api: tuple[FastAPI, PublicationStore, DatasetContract, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No settings means the route stays hidden and never reads the store."""
    app, store, _, publication_id = stock_daily_api
    app.dependency_overrides.pop(get_stock_daily_capacity_gate)
    _disable(monkeypatch)

    def unexpected_store_call(*args: object, **kwargs: object) -> object:
        pytest.fail("disabled stock-daily route must not read publications")

    monkeypatch.setattr(store, "get_publication", unexpected_store_call)
    response = await api_fixtures._request(
        app,
        params=api_fixtures._first_page_params(publication_id),
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


@pytest.mark.asyncio
async def test_flag_only_returns_404(
    stock_daily_api: tuple[FastAPI, PublicationStore, DatasetContract, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The enable flag alone does not satisfy the digest-pinned gate."""
    app, _, _, publication_id = stock_daily_api
    app.dependency_overrides.pop(get_stock_daily_capacity_gate)
    _disable(monkeypatch)
    monkeypatch.setenv(_ENV_FLAG, "true")

    response = await api_fixtures._request(
        app,
        params=api_fixtures._first_page_params(publication_id),
    )

    assert response.status_code == 404


@pytest.mark.parametrize("digest", ["0" * 64, "not-a-sha256", "A" * 64])
@pytest.mark.asyncio
async def test_bad_configured_digest_returns_404(
    stock_daily_api: tuple[FastAPI, PublicationStore, DatasetContract, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    digest: str,
) -> None:
    """A wrong or malformed configured digest fails closed with the uniform 404."""
    app, _, contract, publication_id = stock_daily_api
    app.dependency_overrides.pop(get_stock_daily_capacity_gate)
    path = tmp_path / "capacity.json"
    _write_document(path, _document(contract, publication_id))
    _enable(monkeypatch, path, digest)

    response = await api_fixtures._request(
        app,
        params=api_fixtures._first_page_params(publication_id),
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_missing_attestation_file_returns_404(
    stock_daily_api: tuple[FastAPI, PublicationStore, DatasetContract, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A configured but absent attestation file fails closed."""
    app, _, _, publication_id = stock_daily_api
    app.dependency_overrides.pop(get_stock_daily_capacity_gate)
    _enable(monkeypatch, tmp_path / "missing.json", "a" * 64)

    response = await api_fixtures._request(
        app,
        params=api_fixtures._first_page_params(publication_id),
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_valid_attestation_enables_route_and_keeps_openapi_hidden(
    stock_daily_api: tuple[FastAPI, PublicationStore, DatasetContract, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A valid pinned file enables the route without publishing it in OpenAPI."""
    app, _, contract, publication_id = stock_daily_api
    app.dependency_overrides.pop(get_stock_daily_capacity_gate)
    path = tmp_path / "capacity.json"
    digest = _write_document(path, _document(contract, publication_id))
    _enable(monkeypatch, path, digest)

    response = await api_fixtures._request(
        app,
        params=api_fixtures._first_page_params(publication_id),
    )

    assert response.status_code == 200
    assert response.json()["dataset_id"] == DATASET_ID
    assert "/api/v2/stock-daily/records" not in app.openapi()["paths"]


@pytest.mark.asyncio
async def test_contract_digest_mismatch_returns_404(
    stock_daily_api: tuple[FastAPI, PublicationStore, DatasetContract, str],
) -> None:
    """An attestation for another contract cannot read this publication."""
    app, _, contract, publication_id = stock_daily_api
    _enable_gate_in_app(
        app,
        api_fixtures._capacity_attestation(contract, publication_id).model_copy(
            update={"contract_sha256": "b" * 64}
        ),
    )

    response = await api_fixtures._request(
        app,
        params=api_fixtures._first_page_params(publication_id),
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_publication_pin_mismatch_returns_404(
    stock_daily_api: tuple[FastAPI, PublicationStore, DatasetContract, str],
) -> None:
    """A pinned attestation for another publication cannot read this one."""
    app, _, contract, publication_id = stock_daily_api
    _enable_gate_in_app(
        app,
        api_fixtures._capacity_attestation(contract, PIN_MISMATCH),
    )

    response = await api_fixtures._request(
        app,
        params=api_fixtures._first_page_params(publication_id),
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_window_limit_returns_404_before_snapshot_creation(
    stock_daily_api: tuple[FastAPI, PublicationStore, DatasetContract, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The attested window bound is enforced before the store creates a snapshot."""
    app, store, contract, publication_id = stock_daily_api
    _enable_gate_in_app(
        app,
        api_fixtures._capacity_attestation(
            contract,
            publication_id,
            max_window_days=1,
        ),
    )

    def forbidden_snapshot_creation(*args: object, **kwargs: object) -> object:
        pytest.fail("capacity limits must be enforced before snapshot creation")

    monkeypatch.setattr(store, "create_snapshot", forbidden_snapshot_creation)
    response = await api_fixtures._request(
        app,
        params=api_fixtures._first_page_params(publication_id),
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_page_size_limit_returns_404_before_snapshot_creation(
    stock_daily_api: tuple[FastAPI, PublicationStore, DatasetContract, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The attested page-size bound is enforced before snapshot creation."""
    app, store, contract, publication_id = stock_daily_api
    _enable_gate_in_app(
        app,
        api_fixtures._capacity_attestation(
            contract,
            publication_id,
            max_page_size=1,
        ),
    )

    def forbidden_snapshot_creation(*args: object, **kwargs: object) -> object:
        pytest.fail("capacity limits must be enforced before snapshot creation")

    monkeypatch.setattr(store, "create_snapshot", forbidden_snapshot_creation)
    response = await api_fixtures._request(
        app,
        params=api_fixtures._first_page_params(publication_id, page_size=2),
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_snapshot_page_rechecks_window_and_page_size_limits(
    stock_daily_api: tuple[FastAPI, PublicationStore, DatasetContract, str],
) -> None:
    """Continuation pages reapply both bounds to the stored snapshot values."""
    app, _, contract, publication_id = stock_daily_api
    _enable_gate_in_app(
        app,
        api_fixtures._capacity_attestation(
            contract,
            publication_id,
            max_window_days=3,
            max_page_size=2,
        ),
    )
    first_response = await api_fixtures._request(
        app,
        params=api_fixtures._first_page_params(publication_id, page_size=2),
    )
    assert first_response.status_code == 200
    snapshot_id = first_response.json()["snapshot_id"]
    continuation: dict[str, str] = {
        "market": api_fixtures.MARKET,
        "symbol": api_fixtures.SYMBOL,
        "publication_id": publication_id,
        "snapshot_id": snapshot_id,
    }

    valid_response = await api_fixtures._request(app, params=continuation)
    assert valid_response.status_code == 200

    _enable_gate_in_app(
        app,
        api_fixtures._capacity_attestation(
            contract,
            publication_id,
            max_window_days=2,
            max_page_size=2,
        ),
    )
    window_response = await api_fixtures._request(app, params=continuation)

    _enable_gate_in_app(
        app,
        api_fixtures._capacity_attestation(
            contract,
            publication_id,
            max_window_days=3,
            max_page_size=1,
        ),
    )
    page_size_response = await api_fixtures._request(app, params=continuation)

    assert window_response.status_code == 404
    assert page_size_response.status_code == 404


def _loader_attestation(path: Path, content: bytes) -> StockDailyCapacityAttestation:
    """Load exact bytes through the public loader."""
    digest = _write_bytes(path, content)
    return load_stock_daily_capacity_attestation(path, digest)


def test_loader_rejects_oversized_file(tmp_path: Path) -> None:
    """The loader reads at most one byte beyond its fixed size bound."""
    path = tmp_path / "oversized.json"
    content = b"{" + b" " * MAX_CAPACITY_ATTESTATION_BYTES
    with pytest.raises(StockDailyCapacityError):
        _loader_attestation(path, content)


def test_loader_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    """Duplicate keys are rejected before Pydantic sees the object."""
    contract_hash = "c" * 64
    valid = json.dumps(
        _document_with_hash(contract_hash),
        separators=(",", ":"),
    )
    duplicated = valid.replace(
        '"schema_version":',
        '"schema_version":"stock-daily-capacity-v1","schema_version":',
        1,
    )
    with pytest.raises(StockDailyCapacityError):
        _loader_attestation(tmp_path / "duplicate.json", duplicated.encode("utf-8"))


def test_loader_rejects_unknown_missing_and_invalid_fields(tmp_path: Path) -> None:
    """Unknown fields, missing fields, bounds, and naive timestamps all fail closed."""
    base = _document_with_hash("d" * 64)
    invalid_documents = [
        {**base, "unexpected_notes": "not allowed"},
        {name: value for name, value in base.items() if name != "attested_by"},
        {**base, "max_window_days": 0},
        {**base, "max_window_days": 3_661},
        {**base, "max_page_size": 0},
        {**base, "max_page_size": 1_001},
        {**base, "not_before": "2020-01-01T00:00:00"},
        {**base, "expires_at": "2100-01-01T00:00:00"},
        {**base, "expires_at": "2019-01-01T00:00:00Z"},
        {**base, "publication_id": "A" * 32},
        {**base, "contract_sha256": "not-a-digest"},
        {**base, "dataset_id": "other-dataset"},
        {**base, "source_id": "other.source"},
        {**base, "semantic_version": "stock-daily-raw-v2"},
    ]

    for index, document in enumerate(invalid_documents):
        with pytest.raises(StockDailyCapacityError):
            _loader_attestation(
                tmp_path / f"invalid-{index}.json",
                json.dumps(document, separators=(",", ":")).encode("utf-8"),
            )


def _document_with_hash(contract_hash: str) -> dict[str, object]:
    """Return a loader-valid document without needing a real contract."""
    return {
        "schema_version": "stock-daily-capacity-v1",
        "dataset_id": DATASET_ID,
        "source_id": "akshare.daily",
        "semantic_version": "stock-daily-raw-v1",
        "contract_sha256": contract_hash,
        "publication_id": None,
        "max_window_days": 3_660,
        "max_page_size": 1_000,
        "not_before": "2020-01-01T00:00:00Z",
        "expires_at": "2100-01-01T00:00:00Z",
        "evidence_ref": "docs/operations-stock-daily-publication-capacity.md",
        "attested_by": "offline-test",
    }


@pytest.mark.asyncio
async def test_expired_and_not_yet_valid_attestations_return_404(
    stock_daily_api: tuple[FastAPI, PublicationStore, DatasetContract, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The gate rejects attestations outside their UTC validity window."""
    app, _, contract, publication_id = stock_daily_api
    app.dependency_overrides.pop(get_stock_daily_capacity_gate)
    windows = (
        ("expired.json", "2020-01-01T00:00:00Z", "2021-01-01T00:00:00Z"),
        ("future.json", "2090-01-01T00:00:00Z", "2100-01-01T00:00:00Z"),
    )
    for filename, not_before, expires_at in windows:
        path = tmp_path / filename
        _write_document(
            path,
            _document(
                contract,
                publication_id,
                not_before=not_before,
                expires_at=expires_at,
            ),
        )
        _enable(monkeypatch, path, hashlib.sha256(path.read_bytes()).hexdigest())
        response = await api_fixtures._request(
            app,
            params=api_fixtures._first_page_params(publication_id),
        )
        assert response.status_code == 404


def test_validator_rejects_publication_source_and_semantic_mismatch(
    stock_daily_api: tuple[FastAPI, PublicationStore, DatasetContract, str],
) -> None:
    """Defense-in-depth binding rejects publication drift even if earlier checks pass."""
    _, store, contract, publication_id = stock_daily_api
    publication = store.get_publication(publication_id)
    assert publication is not None
    attestation = api_fixtures._capacity_attestation(contract, publication_id)
    arguments = {
        "contract_hash": canonical_sha256(contract),
        "requested_publication_id": publication_id,
        "start": date(2026, 10, 26),
        "end": date(2026, 10, 29),
        "page_size": 1,
    }

    _require_capacity_attestation(attestation, publication=publication, **arguments)
    for drifted in (
        replace(publication, source_id="other.source"),
        replace(publication, semantic_version="stock-daily-raw-v2"),
        replace(publication, dataset_id="other-dataset"),
    ):
        with pytest.raises(HTTPException) as error:
            _require_capacity_attestation(attestation, publication=drifted, **arguments)
        assert error.value.status_code == 404
