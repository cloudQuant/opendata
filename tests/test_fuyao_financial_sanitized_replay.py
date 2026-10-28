"""Replay six sanitized local Fuyao financial response shapes through the decoder.

The fixtures preserve only the recorded envelope, field/type/null shape, and
the distinctions among three statements and two period modes. Identifiers,
request IDs, values, and timestamps are synthetic. The fixtures do not establish
source authorization, unit semantics, retention rights, or historical coverage.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from opendata.data.providers.ths.endpoints import (
    FINANCIAL_PERIOD_END,
    financial_statement_items,
)
from opendata.data.providers.ths.models.financial_observation_raw import (
    FinancialObservationDecodeResult,
    decode_financial_observation_body,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "fuyao_financial_sanitized_replay"
EXPECTED_CASES = {
    f"{statement}-{periodicity}"
    for statement in ("income", "balance", "cashflow")
    for periodicity in ("annual", "quarterly")
}
# Format-valid fixture token only; this does not assert a real issuer exists.
SYNTHETIC_INSTRUMENT_ID = "999999.SZ"
OBSERVED_AT = datetime(2026, 5, 1, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
_UNIT_MAPPING_VERSION = "synthetic-replay-declaration-v1"
_UNIT_MAPPING_SHA256 = hashlib.sha256(b"synthetic replay only").hexdigest()
_ROW_METADATA_FIELDS = {
    "thscode",
    "ticker",
    "currency",
    "period",
    "fiscal_year",
    "fiscal_period",
    "period_end_ms",
    "report_date_ms",
}


def _type_name(value: object) -> str:
    """Return the JSON scalar's exact Python type name for fixture checks."""
    if value is None:
        return "null"
    return type(value).__name__


def _read_replay(case_id: str) -> tuple[dict[str, Any], bytes, dict[str, Any]]:
    """Read a synthetic fixture and its non-sensitive shape metadata."""
    manifest = json.loads((FIXTURE_DIR / "manifest.json").read_text(encoding="utf-8"))
    case = manifest["cases"][case_id]
    body = (FIXTURE_DIR / f"{case_id}.json").read_bytes()
    assert hashlib.sha256(body).hexdigest() == case["fixture_sha256"]
    payload: dict[str, Any] = json.loads(body)
    return manifest, body, {**case, "payload": payload}


def _decode(
    body: bytes, *, statement_type: str, periodicity: str
) -> FinancialObservationDecodeResult:
    """Call the production decoder with explicitly synthetic declarations."""
    items = financial_statement_items(statement_type)
    return decode_financial_observation_body(
        body,
        expected_instrument_id=SYNTHETIC_INSTRUMENT_ID,
        statement_type=statement_type,
        periodicity=periodicity,
        observed_at=OBSERVED_AT,
        native_units=dict.fromkeys(items, "synthetic-unit-declaration"),
        native_unit_mapping_version_declared=_UNIT_MAPPING_VERSION,
        native_unit_mapping_sha256_declared=_UNIT_MAPPING_SHA256,
    )


def test_six_sanitized_recordings_cover_each_statement_and_period_mode() -> None:
    """The fixture plane retains all six captured report/cadence combinations."""
    manifest = json.loads((FIXTURE_DIR / "manifest.json").read_text(encoding="utf-8"))
    cases = manifest["cases"]

    assert manifest["schema_version"] == 1
    assert "no authorization" in manifest["scope"]
    assert "source_sha256" not in manifest
    assert set(cases) == EXPECTED_CASES
    assert {(case["statement_type"], case["periodicity"]) for case in cases.values()} == {
        (statement, periodicity)
        for statement in ("income", "balance", "cashflow")
        for periodicity in ("annual", "quarterly")
    }
    for case in cases.values():
        assert "source_sha256" not in case
        assert len(case["fixture_sha256"]) == 64


@pytest.mark.parametrize("case_id", sorted(EXPECTED_CASES))
def test_sanitized_recording_matches_real_parser_fields_types_and_time(
    case_id: str,
) -> None:
    """All six captured shapes decode with exact fields and Shanghai dates."""
    _manifest, body, case = _read_replay(case_id)
    payload = case["payload"]
    statement_type = case["statement_type"]
    periodicity = case["periodicity"]
    row = payload["data"]["item"][0]
    items = financial_statement_items(statement_type)

    assert payload["request_id"] == f"synthetic-replay-{case_id}"
    assert row["thscode"] == SYNTHETIC_INSTRUMENT_ID
    assert row["ticker"] == "999999"
    assert row["period"] == periodicity
    assert row["fiscal_year"] == case["synthetic_fiscal_year"]
    assert row["fiscal_period"] == case["fiscal_period"]
    if periodicity == "annual":
        assert row["fiscal_period"] == "FY"
    else:
        assert row["fiscal_period"] in {"Q1", "Q2", "Q3", "Q4"}
    assert set(row) == _ROW_METADATA_FIELDS | set(items)
    assert {key: _type_name(value) for key, value in payload.items()} == case[
        "envelope_field_types"
    ]
    assert {key: _type_name(value) for key, value in payload["data"].items()} == case[
        "data_field_types"
    ]
    assert {key: _type_name(value) for key, value in row.items()} == case["row_field_types"]
    assert {key: _type_name(row[key]) for key in items} == case["item_value_types"]

    decoded = _decode(body, statement_type=statement_type, periodicity=periodicity)
    assert decoded.source_request_id == f"synthetic-replay-{case_id}"
    assert len(decoded.observations) == len(items)
    assert decoded.quarantined_rows == ()
    assert {reference.observation.item_code for reference in decoded.observations} == set(items)

    fiscal_period = row["fiscal_period"]
    month, day = FINANCIAL_PERIOD_END[fiscal_period]
    expected_period = date(row["fiscal_year"], month, day)
    synthetic_disclosure_date = date.fromisoformat(case["synthetic_disclosure_date"])
    assert expected_period.isoformat() == case["synthetic_report_period"]
    assert row["period_end_ms"] == case["synthetic_period_end_ms"]
    assert payload["data"]["timestamp"] == case["synthetic_period_end_ms"]
    expected_timestamp = datetime.fromtimestamp(
        row["period_end_ms"] / 1000, tz=timezone.utc
    ).astimezone(ZoneInfo("Asia/Shanghai"))
    assert expected_timestamp.date() == expected_period
    assert expected_timestamp.timetz().replace(tzinfo=None) == time.min
    assert row["report_date_ms"] == int(
        datetime.combine(
            synthetic_disclosure_date,
            time.min,
            tzinfo=ZoneInfo("Asia/Shanghai"),
        ).timestamp()
        * 1000
    )

    for reference in decoded.observations:
        observation = reference.observation
        assert observation.instrument_id == SYNTHETIC_INSTRUMENT_ID
        assert observation.statement_type == statement_type
        assert observation.periodicity == periodicity
        assert observation.report_period == expected_period
        assert observation.source_disclosure_date == synthetic_disclosure_date
        assert observation.observed_at == OBSERVED_AT.astimezone(timezone.utc)
        assert reference.row_pointer == "/data/item/0"
        assert reference.value_pointer == f"/data/item/0/{observation.item_code}"
        source_type = case["item_value_types"][observation.item_code]
        synthetic_value = row[observation.item_code]
        if synthetic_value is None:
            assert observation.value is None
        elif source_type == "float":
            assert isinstance(observation.value, Decimal)
            assert observation.value == Decimal(str(synthetic_value))
        elif source_type == "int":
            assert type(observation.value) is int
            assert observation.value == synthetic_value
        else:
            pytest.fail("fixture contains an unsupported financial value type")


def test_null_value_on_sanitized_recording_shape_is_preserved() -> None:
    """A test-only synthetic null mutation follows the captured income row shape.

    This mutation does not claim that any of the six source captures contained
    a null financial value.
    """
    _manifest, _body, case = _read_replay("income-annual")
    payload = case["payload"]
    payload["data"]["item"][0]["net_profit"] = None
    null_body = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    decoded = _decode(null_body, statement_type="income", periodicity="annual")
    net_profit = next(
        reference
        for reference in decoded.observations
        if reference.observation.item_code == "net_profit"
    )

    assert net_profit.observation.value is None
    assert net_profit.value_pointer == "/data/item/0/net_profit"
