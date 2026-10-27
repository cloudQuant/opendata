"""Synthetic wire-to-observation acceptance for Fuyao financial responses.

The fixture is deliberately generated in this test. It exercises the real
HTTPX transport surface and the real decoder, but is not a recorded upstream
response, does not archive the body, and does not establish production wiring.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import httpx
import pytest

from opendata.data.providers.ths.endpoints import financial_statement_items
from opendata.data.providers.ths.models.financial_observation_raw import (
    MAX_BODY_BYTES,
    FinancialObservationDecodeError,
    decode_financial_observation_body,
)
from opendata.data.providers.ths.transport.credentials import FuyaoCredentials
from opendata.data.providers.ths.transport.errors import FuyaoError
from opendata.data.providers.ths.transport.http_client import FuyaoHttpClient
from opendata.data.providers.ths.transport.rate_limiter import FuyaoRateLimiter

_HOST = "fuyao.synthetic.invalid"
_ENDPOINT = "/api/a-share/financials/income-statements"
_INSTRUMENT_ID = "600519.SH"
_PERIOD_END_MS = 1735574400000
_DISCLOSURE_MS = 1744819200000
_OBSERVED_AT = datetime(2025, 4, 18, 2, 30, tzinfo=timezone.utc)
_PRECISE_VALUE_TOKEN = "9876543210.123456789012345678"


def _synthetic_financial_body() -> bytes:
    """Build a measured-shape envelope with one exact value and one quarantine row."""
    items = financial_statement_items("income")
    first_row: dict[str, Any] = {
        "thscode": _INSTRUMENT_ID,
        "ticker": "600519",
        "period": "annual",
        "fiscal_year": 2024,
        "fiscal_period": "FY",
        "period_end_ms": _PERIOD_END_MS,
        "report_date_ms": _DISCLOSURE_MS,
        "currency": "CNY",
        **dict.fromkeys(items, 0),
    }
    first_row["net_profit"] = "__EXACT_DECIMAL_TOKEN__"

    missing_disclosure_row = dict(first_row)
    missing_disclosure_row.pop("report_date_ms")
    missing_disclosure_row["net_profit"] = 7

    payload = {
        "code": 0,
        "message": "success",
        "request_id": "synthetic-wire-request-204",
        "data": {
            "timestamp": _PERIOD_END_MS,
            "item": [first_row, missing_disclosure_row],
        },
    }
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    marker = b'"net_profit":"__EXACT_DECIMAL_TOKEN__"'
    assert encoded.count(marker) == 1
    return encoded.replace(marker, f'"net_profit":{_PRECISE_VALUE_TOKEN}'.encode("ascii"))


def _mock_client(
    body: bytes, *, max_response_bytes: int = 32 * 1024 * 1024
) -> tuple[FuyaoHttpClient, list[httpx.Request]]:
    """Create the production client with an in-process-only synthetic transport."""
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            content=body,
            headers={"content-type": "application/json; charset=utf-8"},
        )

    client = FuyaoHttpClient(
        credentials=FuyaoCredentials("fixture-key-not-a-credential", base_url=f"https://{_HOST}"),
        mock_transport=httpx.MockTransport(respond),
        rate_limiter=FuyaoRateLimiter(rate_per_second=1_000.0, burst=1_000),
        max_attempts=1,
        max_response_bytes=max_response_bytes,
        backoff_base_seconds=0,
        backoff_jitter_seconds=0,
        sleep=lambda _seconds: None,
    )
    return client, requests


def test_synthetic_wire_body_decodes_to_exact_observations_and_source_references() -> None:
    """Carry exact synthetic response bytes through transport into Decimal rows."""
    raw_body = _synthetic_financial_body()
    client, requests = _mock_client(raw_body)
    try:
        response = client.get(_ENDPOINT)
    finally:
        client.close()

    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert str(requests[0].url) == f"https://{_HOST}{_ENDPOINT}"
    assert requests[0].headers["X-api-key"] == "fixture-key-not-a-credential"
    assert response.raw_content == raw_body
    assert response.envelope.request_id == "synthetic-wire-request-204"

    legacy_value = response.envelope.items[0]["net_profit"]
    assert type(legacy_value) is float
    assert Decimal(str(legacy_value)) != Decimal(_PRECISE_VALUE_TOKEN)

    items = financial_statement_items("income")
    decoded = decode_financial_observation_body(
        response.raw_content,
        expected_instrument_id=_INSTRUMENT_ID,
        statement_type="income",
        periodicity="annual",
        observed_at=_OBSERVED_AT,
        native_units={item: "CNY/share" if item == "basic_eps" else "CNY" for item in items},
        native_unit_mapping_version_declared="synthetic-fixture-v1",
        native_unit_mapping_sha256_declared=hashlib.sha256(
            b"synthetic unit declaration"
        ).hexdigest(),
    )

    net_profit = next(
        reference
        for reference in decoded.observations
        if reference.observation.item_code == "net_profit"
    )
    assert len(decoded.observations) == len(items)
    assert net_profit.observation.value == Decimal(_PRECISE_VALUE_TOKEN)
    assert decoded.source_request_id == response.envelope.request_id
    assert decoded.body_sha256 == hashlib.sha256(raw_body).hexdigest()
    assert net_profit.body_sha256 == decoded.body_sha256
    assert net_profit.row_pointer == "/data/item/0"
    assert net_profit.value_pointer == "/data/item/0/net_profit"
    assert len(decoded.quarantined_rows) == 1
    quarantine = decoded.quarantined_rows[0]
    assert quarantine.body_sha256 == decoded.body_sha256
    assert quarantine.row_pointer == "/data/item/1"
    assert quarantine.disclosure_pointer is None
    assert quarantine.reason == "disclosure_date_missing"


def test_transport_size_limit_and_relative_path_reject_before_data_can_escape() -> None:
    """Keep transport byte ceilings and endpoint path validation in force."""
    raw_body = _synthetic_financial_body()
    client, requests = _mock_client(raw_body, max_response_bytes=len(raw_body) - 1)
    try:
        with pytest.raises(FuyaoError) as oversized:
            client.get(_ENDPOINT)
        assert oversized.value.category == "response_too_large"
        assert raw_body.decode("utf-8") not in str(oversized.value)
        assert len(requests) == 1

        with pytest.raises(ValueError, match="must start with"):
            client.get("api/a-share/financials/income-statements")
        assert len(requests) == 1
    finally:
        client.close()


def test_decoder_rejects_transport_body_outside_its_own_size_ceiling() -> None:
    """The decoder independently rejects oversized bytes if called without transport."""
    raw_body = b" " * (MAX_BODY_BYTES + 1)

    with pytest.raises(FinancialObservationDecodeError) as oversized:
        decode_financial_observation_body(
            raw_body,
            expected_instrument_id=_INSTRUMENT_ID,
            statement_type="income",
            periodicity="annual",
            observed_at=_OBSERVED_AT,
            native_units={"unused": "CNY"},
            native_unit_mapping_version_declared="synthetic-fixture-v1",
            native_unit_mapping_sha256_declared=hashlib.sha256(b"synthetic").hexdigest(),
        )

    assert oversized.value.code == "body_too_large"
