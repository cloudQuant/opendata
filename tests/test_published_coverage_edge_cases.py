"""Fail-closed edge cases for evidence-bound published coverage reads."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import create_engine

from opendata.api import published_data as published_data_module
from opendata.api.dependencies import Principal
from opendata.api.published_access import DatasetGrantRegistry
from opendata.api.published_data import get_dataset_coverage_evidence_registry_loader
from opendata.data.coverage_evidence_registry import (
    DatasetCoverageEvidence,
    DatasetCoverageEvidenceRegistry,
)
from opendata.data.dataset_contract_registry import (
    DatasetContractRegistry,
    canonical_sha256,
)
from opendata.data.instrument_reference import InstrumentReferenceRegistry
from opendata.data.platform_contracts import DatasetContract
from opendata.data.providers.ths.index_daily_publication import (
    INDEX_SOURCE_ID,
    build_index_daily_contract,
)
from opendata.models.user import User, UserRole
from opendata.pipeline.publication_store import (
    MAX_COVERAGE_RECORDS,
    PUBLICATION_METADATA,
    PublicationStore,
)
from tests.test_published_data_api import (
    _grant,
    _index_coverage_evidence,
    _index_record,
    _make_app,
    _make_index_app,
    _publish_index_contract,
    _request,
    _visible_record_set_pin,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from opendata.data.platform_contracts import DatasetContract


@pytest.fixture
def coverage_store(tmp_path: Path) -> Iterator[tuple[PublicationStore, DatasetContract]]:
    """Create a file-backed SQLite store owned by this test fixture only."""
    engine = create_engine(f"sqlite:///{tmp_path / 'published-coverage-edge.db'}")
    PUBLICATION_METADATA.create_all(engine)
    store = PublicationStore(engine, cursor_signing_key=b"coverage-edge-signing-key-32-bytes!")
    try:
        yield store, build_index_daily_contract("000300.SH")
    finally:
        engine.dispose()


def _pinned_index_evidence(
    store: PublicationStore,
    publication_id: str,
    contract: DatasetContract,
    *,
    start: date,
    end_exclusive: date,
    open_dates: tuple[date, ...],
) -> DatasetCoverageEvidence:
    """Build synthetic schema-2 evidence for a fixture publication only."""
    return _index_coverage_evidence(
        publication_id,
        start=start,
        end_exclusive=end_exclusive,
        open_dates=open_dates,
        visible_record_set=_visible_record_set_pin(store, publication_id, contract),
    )


@pytest.mark.asyncio
async def test_coverage_denial_happens_before_evidence_or_publication_reads(
    coverage_store: tuple[PublicationStore, DatasetContract],
) -> None:
    """A key lacking coverage.read cannot trigger evidence or warehouse access."""
    store, contract = coverage_store
    user = User(
        id=12,
        username="coverage-denied",
        email="coverage-denied@example.test",
        hashed_password="not-a-real-password-hash",
        role=UserRole.USER,
        is_active=True,
    )
    principal = Principal(user=user, api_key_id=12, scopes=("*", "data.read"))

    def fail_if_called(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("unauthorized coverage request touched publication data")

    app = _make_app(
        store,
        DatasetGrantRegistry((_grant(contract.dataset_id, INDEX_SOURCE_ID),)),
        DatasetContractRegistry((contract,)),
        principal=principal,
    )
    app.dependency_overrides[get_dataset_coverage_evidence_registry_loader] = lambda: fail_if_called
    setattr(store, "get_publication", fail_if_called)
    setattr(store, "list_datasets", fail_if_called)
    setattr(store, "read_coverage_record_set", fail_if_called)

    response = await _request(
        app,
        "GET",
        f"/api/v2/datasets/{contract.dataset_id}/coverage",
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "dataset_unavailable"


@pytest.mark.asyncio
async def test_coverage_pin_mismatch_returns_unknown_without_scanning_facts(
    coverage_store: tuple[PublicationStore, DatasetContract],
) -> None:
    """Evidence for an older immutable publication cannot authorize a newer scan."""
    store, _ = coverage_store
    day = date(2026, 10, 21)
    end_exclusive = day + timedelta(days=1)
    first, contract = _publish_index_contract(
        store,
        (_index_record("000300.SH", day, 4_000.0),),
        idempotency_key="coverage-edge-first-pin",
    )
    second, _ = _publish_index_contract(
        store,
        (_index_record("000300.SH", day, 4_001.0),),
        idempotency_key="coverage-edge-second-pin",
    )
    evidence = _pinned_index_evidence(
        store,
        first.publication_id,
        contract,
        start=day,
        end_exclusive=end_exclusive,
        open_dates=(day,),
    )

    def fail_if_scanned(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("publication facts were scanned without an exact evidence pin")

    app = _make_index_app(
        store,
        contract,
        DatasetCoverageEvidenceRegistry((evidence,)),
    )
    setattr(store, "read_coverage_record_set", fail_if_scanned)

    response = await _request(
        app,
        "GET",
        f"/api/v2/datasets/{contract.dataset_id}/coverage",
        params={
            "publication_id": second.publication_id,
            "start_date": day.isoformat(),
            "end_date": end_exclusive.isoformat(),
        },
    )

    assert response.status_code == 200
    coverage = response.json()["coverage"]
    assert response.json()["publication_id"] == second.publication_id
    assert coverage["status"] == "unknown"
    assert coverage["observed"] is None
    assert coverage["evidence_refs"] == []


@pytest.mark.asyncio
async def test_schema1_coverage_evidence_stays_unknown_without_scanning(
    coverage_store: tuple[PublicationStore, DatasetContract],
) -> None:
    """Legacy evidence remains parseable but cannot authorize completeness."""
    store, _ = coverage_store
    day = date(2026, 10, 21)
    end_exclusive = day + timedelta(days=1)
    publication, contract = _publish_index_contract(
        store,
        (_index_record("000300.SH", day, 4_000.0),),
        idempotency_key="coverage-edge-schema-one-evidence",
    )
    evidence = _index_coverage_evidence(
        publication.publication_id,
        start=day,
        end_exclusive=end_exclusive,
        open_dates=(day,),
    )

    def fail_if_scanned(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("schema 1 evidence cannot trigger a coverage scan")

    app = _make_index_app(
        store,
        contract,
        DatasetCoverageEvidenceRegistry((evidence,)),
    )
    setattr(store, "read_coverage_record_set", fail_if_scanned)
    response = await _request(
        app,
        "GET",
        f"/api/v2/datasets/{contract.dataset_id}/coverage",
        params={"publication_id": publication.publication_id},
    )

    assert response.status_code == 200
    coverage = response.json()["coverage"]
    assert coverage["status"] == "unknown"
    assert coverage["observed"] is None
    assert "Schema 1" in coverage["limitation"]


@pytest.mark.asyncio
async def test_full_record_set_digest_mismatch_stays_unknown(
    coverage_store: tuple[PublicationStore, DatasetContract],
) -> None:
    """An exact publication with altered sidecar digest cannot become COMPLETE."""
    store, _ = coverage_store
    day = date(2026, 10, 21)
    end_exclusive = day + timedelta(days=1)
    publication, contract = _publish_index_contract(
        store,
        (_index_record("000300.SH", day, 4_000.0),),
        idempotency_key="coverage-edge-mismatched-record-set-digest",
    )
    evidence = _pinned_index_evidence(
        store,
        publication.publication_id,
        contract,
        start=day,
        end_exclusive=end_exclusive,
        open_dates=(day,),
    )
    assert evidence.visible_record_set is not None
    mismatched_pin = replace(evidence.visible_record_set, sha256="f" * 64)
    evidence = replace(evidence, visible_record_set=mismatched_pin)
    app = _make_index_app(
        store,
        contract,
        DatasetCoverageEvidenceRegistry((evidence,)),
    )

    response = await _request(
        app,
        "GET",
        f"/api/v2/datasets/{contract.dataset_id}/coverage",
        params={
            "publication_id": publication.publication_id,
            "start_date": day.isoformat(),
            "end_date": end_exclusive.isoformat(),
        },
    )

    assert response.status_code == 200
    coverage = response.json()["coverage"]
    assert coverage["status"] == "unknown"
    assert coverage["observed"] is None
    assert "does not match the prior operator pin" in coverage["limitation"]


@pytest.mark.asyncio
@pytest.mark.parametrize("future_fact", ("requested_range", "lifecycle", "scan_scope"))
async def test_future_history_claims_stay_unknown_without_scanning(
    coverage_store: tuple[PublicationStore, DatasetContract],
    monkeypatch: pytest.MonkeyPatch,
    future_fact: str,
) -> None:
    """Future observations, status dates, and scan bounds cannot establish history."""
    store, _ = coverage_store
    today = date(2026, 10, 27)
    day = today - timedelta(days=1)
    end_exclusive = today
    monkeypatch.setattr(published_data_module, "_shanghai_business_date", lambda: today)
    publication, contract = _publish_index_contract(
        store,
        (_index_record("000300.SH", day, 4_000.0),),
        idempotency_key=f"coverage-edge-future-{future_fact}",
    )
    evidence = _pinned_index_evidence(
        store,
        publication.publication_id,
        contract,
        start=day,
        end_exclusive=end_exclusive,
        open_dates=(day,),
    )
    request_params = {"publication_id": publication.publication_id}
    if future_fact == "requested_range":
        request_params["start_date"] = (today + timedelta(days=1)).isoformat()
        request_params["end_date"] = (today + timedelta(days=2)).isoformat()
    elif future_fact == "lifecycle":
        reference = evidence.instrument_registry
        future_lifecycle = replace(reference.lifecycles[0], status_as_of=today + timedelta(days=1))
        future_registry = InstrumentReferenceRegistry(
            registry_version=reference.registry_version,
            evidence_ref=reference.evidence_ref,
            lifecycles=(future_lifecycle,),
            aliases=reference.aliases,
            schema_version=reference.schema_version,
        )
        evidence = replace(evidence, instrument_registry=future_registry)
    else:
        evidence = replace(
            evidence,
            publication_scope_end_exclusive=today + timedelta(days=2),
        )

    def fail_if_scanned(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("future-bound evidence cannot trigger a coverage scan")

    app = _make_index_app(
        store,
        contract,
        DatasetCoverageEvidenceRegistry((evidence,)),
    )
    setattr(store, "read_coverage_record_set", fail_if_scanned)
    response = await _request(
        app,
        "GET",
        f"/api/v2/datasets/{contract.dataset_id}/coverage",
        params=request_params,
    )

    assert response.status_code == 200
    coverage = response.json()["coverage"]
    assert coverage["status"] == "unknown"
    assert coverage["observed"] is None
    assert (
        "Future lifecycle status, scan scope, or requested business dates" in coverage["limitation"]
    )


@pytest.mark.asyncio
async def test_future_observation_in_exactly_pinned_publication_stays_unknown(
    coverage_store: tuple[PublicationStore, DatasetContract],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pinned future-dated fact cannot leak into an otherwise historical response."""
    store, _ = coverage_store
    today = date(2026, 10, 27)
    previous_day = today - timedelta(days=1)
    future_day = today + timedelta(days=1)
    end_exclusive = today + timedelta(days=1)
    monkeypatch.setattr(published_data_module, "_shanghai_business_date", lambda: today)
    publication, contract = _publish_index_contract(
        store,
        (
            _index_record("000300.SH", previous_day, 4_000.0),
            _index_record("000300.SH", future_day, 4_010.0),
        ),
        idempotency_key="coverage-edge-future-visible-fact",
    )
    evidence = _pinned_index_evidence(
        store,
        publication.publication_id,
        contract,
        start=previous_day,
        end_exclusive=end_exclusive,
        open_dates=(previous_day, today),
    )
    app = _make_index_app(
        store,
        contract,
        DatasetCoverageEvidenceRegistry((evidence,)),
    )

    response = await _request(
        app,
        "GET",
        f"/api/v2/datasets/{contract.dataset_id}/coverage",
        params={
            "publication_id": publication.publication_id,
            "start_date": previous_day.isoformat(),
            "end_date": end_exclusive.isoformat(),
        },
    )

    assert response.status_code == 200
    coverage = response.json()["coverage"]
    assert coverage["status"] == "unknown"
    assert coverage["observed"] is None
    assert "outside its independently evidenced scan scope" in coverage["limitation"]


@pytest.mark.asyncio
async def test_noncanonical_index_contract_cannot_use_matching_evidence(
    coverage_store: tuple[PublicationStore, DatasetContract],
) -> None:
    """A valid pin alone cannot bless a contract outside the exact index allowlist."""
    store, _ = coverage_store
    day = date(2026, 10, 21)
    end_exclusive = day + timedelta(days=1)
    trusted_contract = build_index_daily_contract("000300.SH")
    contract = trusted_contract.model_copy(update={"semantic_version": "9.9.9"})
    publication = store.publish(
        dataset_id=contract.dataset_id,
        source_id=INDEX_SOURCE_ID,
        schema_version=contract.schema_version,
        semantic_version=contract.semantic_version,
        unit="native",
        adjustment_basis=contract.adjustment_basis,
        quality={"status": "passed", "report_id": "DQ-COVERAGE-EDGE", "issue_count": 0},
        provenance={"contract_sha256": canonical_sha256(contract)},
        idempotency_key="coverage-edge-noncanonical-contract",
        records=(_index_record("000300.SH", day, 4_000.0),),
    )
    evidence = _pinned_index_evidence(
        store,
        publication.publication_id,
        contract,
        start=day,
        end_exclusive=end_exclusive,
        open_dates=(day,),
    )

    def fail_if_scanned(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("noncanonical contracts must be rejected before fact scanning")

    app = _make_app(
        store,
        DatasetGrantRegistry((_grant(contract.dataset_id, INDEX_SOURCE_ID),)),
        DatasetContractRegistry((contract,)),
        coverage_evidence=DatasetCoverageEvidenceRegistry((evidence,)),
    )
    setattr(store, "read_coverage_record_set", fail_if_scanned)

    response = await _request(
        app,
        "GET",
        f"/api/v2/datasets/{contract.dataset_id}/coverage",
        params={
            "publication_id": publication.publication_id,
            "start_date": day.isoformat(),
            "end_date": end_exclusive.isoformat(),
        },
    )

    assert response.status_code == 200
    coverage = response.json()["coverage"]
    assert coverage["status"] == "unknown"
    assert coverage["observed"] is None
    assert "outside the trusted single-index coverage scope" in coverage["limitation"]
    assert f"operator-attestation-sha256:{evidence.evidence_sha256}" in coverage["evidence_refs"]


@pytest.mark.asyncio
async def test_coverage_read_missing_pinned_publication_is_sanitized_unavailable(
    coverage_store: tuple[PublicationStore, DatasetContract],
) -> None:
    """A disappearing publication between metadata and fact reads fails closed."""
    store, _ = coverage_store
    day = date(2026, 10, 21)
    end_exclusive = day + timedelta(days=1)
    publication, contract = _publish_index_contract(
        store,
        (_index_record("000300.SH", day, 4_000.0),),
        idempotency_key="coverage-edge-missing-facts",
    )
    evidence = _pinned_index_evidence(
        store,
        publication.publication_id,
        contract,
        start=day,
        end_exclusive=end_exclusive,
        open_dates=(day,),
    )
    app = _make_index_app(
        store,
        contract,
        DatasetCoverageEvidenceRegistry((evidence,)),
    )

    def missing_pin(
        _dataset_id: str,
        _publication_id: str,
        *,
        contract_sha256: str,
        limit: int = MAX_COVERAGE_RECORDS,
    ) -> None:
        assert contract_sha256 == canonical_sha256(contract)
        assert limit == MAX_COVERAGE_RECORDS
        return None

    setattr(store, "read_coverage_record_set", missing_pin)

    response = await _request(
        app,
        "GET",
        f"/api/v2/datasets/{contract.dataset_id}/coverage",
        params={
            "publication_id": publication.publication_id,
            "start_date": day.isoformat(),
            "end_date": end_exclusive.isoformat(),
        },
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "published_data_unavailable"
    assert str(store._engine.url) not in response.text


@pytest.mark.asyncio
async def test_malformed_coverage_fact_is_reported_unknown_not_complete(
    coverage_store: tuple[PublicationStore, DatasetContract],
) -> None:
    """A persisted field that violates the pinned schema cannot produce completeness."""
    store, _ = coverage_store
    day = date(2026, 10, 21)
    end_exclusive = day + timedelta(days=1)
    publication, contract = _publish_index_contract(
        store,
        (_index_record("000300.SH", day, 4_000.0),),
        idempotency_key="coverage-edge-malformed-fact",
    )
    evidence = _pinned_index_evidence(
        store,
        publication.publication_id,
        contract,
        start=day,
        end_exclusive=end_exclusive,
        open_dates=(day,),
    )
    app = _make_index_app(
        store,
        contract,
        DatasetCoverageEvidenceRegistry((evidence,)),
    )
    read_record_set = store.read_coverage_record_set

    def malformed_records(
        dataset_id: str,
        publication_id: str,
        *,
        contract_sha256: str,
        limit: int = MAX_COVERAGE_RECORDS,
    ) -> Any:
        record_set = read_record_set(
            dataset_id,
            publication_id,
            contract_sha256=contract_sha256,
            limit=limit,
        )
        assert record_set is not None and len(record_set.records) == 1
        malformed_payload = {**record_set.records[0].payload, "close": "not-a-number"}
        return replace(
            record_set,
            records=(replace(record_set.records[0], payload=malformed_payload),),
        )

    setattr(store, "read_coverage_record_set", malformed_records)

    response = await _request(
        app,
        "GET",
        f"/api/v2/datasets/{contract.dataset_id}/coverage",
        params={
            "publication_id": publication.publication_id,
            "start_date": day.isoformat(),
            "end_date": end_exclusive.isoformat(),
        },
    )

    assert response.status_code == 200
    coverage = response.json()["coverage"]
    assert coverage["status"] == "unknown"
    assert coverage["observed"] is None
    assert "malformed facts" in coverage["limitation"]


@pytest.mark.asyncio
async def test_observation_outside_attested_scan_scope_is_not_counted(
    coverage_store: tuple[PublicationStore, DatasetContract],
) -> None:
    """An otherwise valid row beyond the signed scan interval invalidates coverage."""
    store, _ = coverage_store
    observed_day = date(2026, 10, 21)
    scope_start = observed_day + timedelta(days=1)
    end_exclusive = scope_start + timedelta(days=1)
    publication, contract = _publish_index_contract(
        store,
        (_index_record("000300.SH", observed_day, 4_000.0),),
        idempotency_key="coverage-edge-out-of-scope-fact",
    )
    evidence = _pinned_index_evidence(
        store,
        publication.publication_id,
        contract,
        start=scope_start,
        end_exclusive=end_exclusive,
        open_dates=(scope_start,),
    )
    app = _make_index_app(
        store,
        contract,
        DatasetCoverageEvidenceRegistry((evidence,)),
    )

    response = await _request(
        app,
        "GET",
        f"/api/v2/datasets/{contract.dataset_id}/coverage",
        params={
            "publication_id": publication.publication_id,
            "start_date": observed_day.isoformat(),
            "end_date": end_exclusive.isoformat(),
        },
    )

    assert response.status_code == 200
    coverage = response.json()["coverage"]
    assert coverage["status"] == "unknown"
    assert coverage["observed"] is None
    assert "outside its independently evidenced scan scope" in coverage["limitation"]


@pytest.mark.asyncio
async def test_incomplete_calendar_evidence_maps_to_unknown_with_reason(
    coverage_store: tuple[PublicationStore, DatasetContract],
) -> None:
    """A pinned but uncertified calendar cannot yield a complete history claim."""
    store, _ = coverage_store
    day = date(2026, 10, 21)
    end_exclusive = day + timedelta(days=1)
    publication, contract = _publish_index_contract(
        store,
        (_index_record("000300.SH", day, 4_000.0),),
        idempotency_key="coverage-edge-incomplete-calendar",
    )
    evidence = _pinned_index_evidence(
        store,
        publication.publication_id,
        contract,
        start=day,
        end_exclusive=end_exclusive,
        open_dates=(day,),
    )
    incomplete_calendar = replace(evidence.calendar, daily_classification_complete=False)
    evidence = replace(evidence, calendar=incomplete_calendar)
    app = _make_index_app(
        store,
        contract,
        DatasetCoverageEvidenceRegistry((evidence,)),
    )

    response = await _request(
        app,
        "GET",
        f"/api/v2/datasets/{contract.dataset_id}/coverage",
        params={
            "publication_id": publication.publication_id,
            "start_date": day.isoformat(),
            "end_date": end_exclusive.isoformat(),
        },
    )

    assert response.status_code == 200
    coverage = response.json()["coverage"]
    assert coverage["status"] == "unknown"
    assert coverage["scope"] == "business_history"
    assert "does not certify a complete daily" in coverage["limitation"]
    assert coverage["observed"] == {
        "start": day.isoformat(),
        "end": end_exclusive.isoformat(),
    }


@pytest.mark.asyncio
async def test_coverage_without_explicit_range_uses_reviewed_lifecycle_and_scan_bounds(
    coverage_store: tuple[PublicationStore, DatasetContract],
) -> None:
    """An omitted window resolves only to the registered lifecycle and pin scope."""
    store, _ = coverage_store
    day = date(2026, 10, 21)
    end_exclusive = day + timedelta(days=1)
    publication, contract = _publish_index_contract(
        store,
        (_index_record("000300.SH", day, 4_000.0),),
        idempotency_key="coverage-edge-default-window",
    )
    evidence = _pinned_index_evidence(
        store,
        publication.publication_id,
        contract,
        start=day,
        end_exclusive=end_exclusive,
        open_dates=(day,),
    )
    app = _make_index_app(
        store,
        contract,
        DatasetCoverageEvidenceRegistry((evidence,)),
    )

    response = await _request(
        app,
        "GET",
        f"/api/v2/datasets/{contract.dataset_id}/coverage",
        params={"publication_id": publication.publication_id},
    )

    assert response.status_code == 200
    assert response.json()["coverage"]["status"] == "complete"
    assert response.json()["coverage"]["requested"] == {
        "start": day.isoformat(),
        "end": end_exclusive.isoformat(),
    }


@pytest.mark.asyncio
async def test_coverage_rejects_ambiguous_and_reversed_date_windows(
    coverage_store: tuple[PublicationStore, DatasetContract],
) -> None:
    """Coverage accepts either no date bounds or one valid half-open interval."""
    store, _ = coverage_store
    day = date(2026, 10, 21)
    end_exclusive = day + timedelta(days=1)
    _, contract = _publish_index_contract(
        store,
        (_index_record("000300.SH", day, 4_000.0),),
        idempotency_key="coverage-edge-invalid-window",
    )
    app = _make_index_app(store, contract, DatasetCoverageEvidenceRegistry.empty())

    one_sided = await _request(
        app,
        "GET",
        f"/api/v2/datasets/{contract.dataset_id}/coverage",
        params={"start_date": day.isoformat()},
    )
    reversed_window = await _request(
        app,
        "GET",
        f"/api/v2/datasets/{contract.dataset_id}/coverage",
        params={
            "start_date": end_exclusive.isoformat(),
            "end_date": day.isoformat(),
        },
    )
    unknown_filter = await _request(
        app,
        "GET",
        f"/api/v2/datasets/{contract.dataset_id}/coverage",
        params={"unexpected_filter": "symbol=000300.SH"},
    )

    assert one_sided.status_code == 422
    assert one_sided.json()["error"]["code"] == "invalid_request"
    assert reversed_window.status_code == 422
    assert reversed_window.json()["error"]["code"] == "invalid_request"
    assert unknown_filter.status_code == 422
    assert unknown_filter.json()["error"]["code"] == "unsupported_query"


@pytest.mark.asyncio
async def test_range_before_known_lifecycle_is_not_applicable(
    coverage_store: tuple[PublicationStore, DatasetContract],
) -> None:
    """A verified pre-listing window is not mislabeled as a missing-data gap."""
    store, _ = coverage_store
    lifecycle_start = date(2026, 10, 21)
    requested_start = lifecycle_start - timedelta(days=2)
    publication, contract = _publish_index_contract(
        store,
        (_index_record("000300.SH", lifecycle_start, 4_000.0),),
        idempotency_key="coverage-edge-pre-listing-window",
    )
    evidence = _pinned_index_evidence(
        store,
        publication.publication_id,
        contract,
        start=lifecycle_start,
        end_exclusive=lifecycle_start + timedelta(days=1),
        open_dates=(lifecycle_start,),
    )
    app = _make_index_app(
        store,
        contract,
        DatasetCoverageEvidenceRegistry((evidence,)),
    )

    response = await _request(
        app,
        "GET",
        f"/api/v2/datasets/{contract.dataset_id}/coverage",
        params={
            "publication_id": publication.publication_id,
            "start_date": requested_start.isoformat(),
            "end_date": lifecycle_start.isoformat(),
        },
    )

    assert response.status_code == 200
    coverage = response.json()["coverage"]
    assert coverage["status"] == "not_applicable"
    assert coverage["scope"] == "not_applicable"
    assert coverage["observed"] is None
    assert coverage["missing_ranges"] == []
    assert "does not overlap" in coverage["limitation"]
