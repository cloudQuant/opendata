"""Writer integration for contract-authorized market/symbol selectors."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

import opendata.pipeline.contract_publication as contract_publication
from opendata.data.dataset_contract_registry import DatasetContractRegistry, canonical_sha256
from opendata.data.platform_contracts import DatasetContract, FieldContract, SourceIdentity
from opendata.data.providers.sina.financial_statement_publication import (
    build_sina_fzb_observation_contract,
)
from opendata.pipeline.contract_publication import (
    ContractPublicationError,
    ValidatedPublicationWriter,
)
from opendata.pipeline.run_identity import RunRequest, ScheduleMetadata, TaskDefinition
from opendata.pipeline.run_service import NormalizedBatch, NormalizedRecord, PublicationReceipt

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

UTC = timezone.utc
NOW = datetime(2026, 10, 28, 2, 0, tzinfo=UTC)
MARKET = "CN-SH"
SYMBOL = "SSE:600519"
TRADE_DATE = "2026-10-27"


def _market_contract(*, filter_fields: tuple[str, ...] = ("market", "symbol")) -> DatasetContract:
    """Build an explicit daily-series contract for writer selector checks."""
    return DatasetContract(
        dataset_id="cn-equity-stock-daily",
        schema_version="1.0.0",
        semantic_version="stock-daily-raw-v1",
        asset_class="equity",
        frequency="1d",
        record_kind="time_series",
        fields=(
            FieldContract(name="market", data_type="string"),
            FieldContract(name="symbol", data_type="string"),
            FieldContract(name="trade_date", data_type="date"),
            FieldContract(name="close", data_type="number"),
        ),
        business_key=("market", "symbol", "trade_date"),
        ordering=("trade_date", "market", "symbol"),
        filter_fields=filter_fields,
        time_field="trade_date",
        temporal_semantics="business_date",
        source=SourceIdentity(source_id="akshare.daily", source="akshare"),
        field_units={"close": "CNY/share"},
        adjustment_basis="raw",
        field_adjustments={"close": "raw"},
        timezone="Asia/Shanghai",
        pit_policy="unsupported",
    )


def _market_batch(
    contract: DatasetContract,
    *,
    market: object = MARKET,
    symbol: object = SYMBOL,
    business_key: str | None = None,
) -> NormalizedBatch:
    """Build one batch whose key normally follows its exact payload values."""
    payload: dict[str, object] = {
        "market": market,
        "symbol": symbol,
        "trade_date": TRADE_DATE,
        "close": 123.45,
    }
    key = business_key or json.dumps(
        [market, symbol, TRADE_DATE], ensure_ascii=False, separators=(",", ":")
    )
    return NormalizedBatch(
        records=(
            NormalizedRecord(
                business_key=key,
                payload=payload,
                business_date=date.fromisoformat(TRADE_DATE),
            ),
        ),
        schema_version=contract.schema_version,
        semantic_version=contract.semantic_version,
        unit="per-field",
        adjustment_basis=contract.adjustment_basis,
        quality={},
        provenance={"adapter": "stock-daily-v1"},
    )


def _sina_batch(contract: DatasetContract) -> NormalizedBatch:
    """Build one typed source-native row retaining the established selectors."""
    capture_id = "a" * 64
    payload: dict[str, object] = {
        "instrument_id": "SSE:600519",
        "capture_id": capture_id,
        "statement_code": "fzb",
        "report_period": "2026-09-30",
        "source_paper_code": "600519",
        "published_on": "2026-10-27",
        "publication_precision": "day",
        "source_update_epoch_seconds": 1_793_001_600,
        "source_updated_at": "2026-10-27T02:00:00Z",
        "source_updated_at_timezone": "UTC",
        "retrieved_at_utc": NOW.isoformat().replace("+00:00", "Z"),
        "currency": "CNY",
        "period_metadata": {},
        "report_metadata": {},
        "entries": [],
    }
    key = json.dumps(
        ["SSE:600519", capture_id, "fzb", "2026-09-30"],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return NormalizedBatch(
        records=(
            NormalizedRecord(
                business_key=key,
                payload=payload,
                observed_at=NOW,
            ),
        ),
        schema_version=contract.schema_version,
        semantic_version=contract.semantic_version,
        unit="per-field",
        adjustment_basis=contract.adjustment_basis,
        quality={},
        provenance={"representation": "derived_from_parsed_response"},
    )


def _patch_publication_store(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """Capture validated store inputs without opening a connection or database."""
    calls: list[dict[str, object]] = []

    def publish(_connection: Connection, **kwargs: object) -> SimpleNamespace:
        calls.append(kwargs)
        dataset_id = kwargs["dataset_id"]
        records = kwargs["records"]
        assert isinstance(dataset_id, str)
        assert isinstance(records, tuple)
        return SimpleNamespace(
            publication_id="publication-test",
            dataset_id=dataset_id,
            record_count=len(records),
        )

    monkeypatch.setattr(contract_publication, "publish_in_connection", publish)
    return calls


def _write(
    contract: DatasetContract,
    batch: NormalizedBatch,
    *,
    provider: str,
) -> PublicationReceipt:
    """Run the writer callback against an inert connection token."""
    writer = ValidatedPublicationWriter(DatasetContractRegistry([contract]))
    task = TaskDefinition(
        job_id="stock-daily",
        version="v1",
        dataset_id=contract.dataset_id,
        provider=provider,
        model="stock-daily-v1",
        schedule=ScheduleMetadata(kind="cron", expression="0 16 * * 1-5"),
        enabled=False,
    )
    return writer(
        cast("Connection", object()),
        task,
        RunRequest(run_type="manual", idempotency_key="writer-selector-test"),
        batch,
        "writer-selector-test",
    )


def test_explicit_market_symbol_profile_materializes_only_validated_payload_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _market_contract()
    calls = _patch_publication_store(monkeypatch)

    receipt = _write(contract, _market_batch(contract), provider="akshare")

    assert receipt.dataset_id == contract.dataset_id
    assert len(calls) == 1
    provenance = calls[0]["provenance"]
    assert isinstance(provenance, dict)
    assert provenance["contract_sha256"] == canonical_sha256(contract)
    records = calls[0]["records"]
    assert isinstance(records, tuple)
    record = records[0]
    assert record.business_key == json.dumps(
        [MARKET, SYMBOL, TRADE_DATE], ensure_ascii=False, separators=(",", ":")
    )
    assert record.payload["market"] == MARKET
    assert record.payload["symbol"] == SYMBOL
    assert record.market == MARKET
    assert record.symbol == SYMBOL
    assert record.instrument_id is None
    assert record.capture_id is None


def test_market_selector_is_not_derived_from_a_mismatched_business_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _market_contract()
    calls = _patch_publication_store(monkeypatch)
    batch = _market_batch(
        contract,
        business_key=json.dumps(
            ["CN-SZ", SYMBOL, TRADE_DATE], ensure_ascii=False, separators=(",", ":")
        ),
    )

    with pytest.raises(
        ContractPublicationError,
        match="^record business key does not match contract fields$",
    ):
        _write(contract, batch, provider="akshare")

    assert calls == []


@pytest.mark.parametrize(
    ("market", "symbol"),
    [("CN-SH*", SYMBOL), (" CN-SH", SYMBOL), (MARKET, "SSE:600519?")],
)
def test_reserved_profile_rejects_invalid_selector_values_before_store(
    monkeypatch: pytest.MonkeyPatch,
    market: str,
    symbol: str,
) -> None:
    contract = _market_contract()
    calls = _patch_publication_store(monkeypatch)

    with pytest.raises(
        ContractPublicationError,
        match="^record selector values are invalid$",
    ):
        _write(
            contract,
            _market_batch(contract, market=market, symbol=symbol),
            provider="akshare",
        )

    assert calls == []


def test_dataset_profile_keeps_market_and_symbol_as_payload_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _market_contract(filter_fields=("market",))
    calls = _patch_publication_store(monkeypatch)

    _write(contract, _market_batch(contract), provider="akshare")

    assert len(calls) == 1
    records = calls[0]["records"]
    assert isinstance(records, tuple)
    record = records[0]
    assert record.market is None
    assert record.symbol is None
    assert record.instrument_id is None
    assert record.capture_id is None


def test_sina_capture_profile_keeps_legacy_pair_and_contract_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = build_sina_fzb_observation_contract()
    calls = _patch_publication_store(monkeypatch)

    _write(contract, _sina_batch(contract), provider="sina")

    assert len(calls) == 1
    provenance = calls[0]["provenance"]
    assert isinstance(provenance, dict)
    assert provenance["contract_sha256"] == canonical_sha256(contract)
    records = calls[0]["records"]
    assert isinstance(records, tuple)
    record = records[0]
    assert record.instrument_id == "SSE:600519"
    assert record.capture_id == "a" * 64
    assert record.market is None
    assert record.symbol is None


def test_malformed_reserved_market_profile_is_not_downgraded_to_dataset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _market_contract().model_copy(
        update={"record_kind": "table", "temporal_semantics": "none", "time_field": None}
    )
    calls = _patch_publication_store(monkeypatch)

    with pytest.raises(
        ContractPublicationError,
        match="^dataset selection profile is invalid$",
    ):
        _write(contract, _market_batch(contract), provider="akshare")

    assert calls == []
