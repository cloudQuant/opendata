"""Malformed calendar, expectation, retention, and publication evidence edges."""

from __future__ import annotations

import json
from datetime import date
from typing import TYPE_CHECKING

import pytest

from opendata.pipeline.stock_daily_gap_evidence_bundle import (
    MAX_DATE_ENTRIES_PER_SERIES,
    StockDailyGapEvidenceError,
    load_stock_daily_gap_evidence_bundle,
)
from tests.test_stock_daily_gap_evidence_bundle import (
    _fixture,
    _json_bytes,
    _repin_manifest,
)

if TYPE_CHECKING:
    from collections.abc import Callable


CALENDAR_REF = "calendar:600519:v1"
EXPECTATION_REF = "expectation:600519:v1"
PUBLICATION_REF = "publication-content:600519:v1"


def _mutated_artifact(
    reference: str,
    mutate: Callable[[dict[str, object]], None],
) -> tuple[bytes, dict[str, bytes]]:
    """Return the established complete fixture with one artifact repinned."""
    manifest, artifacts, _ = _fixture()
    document = json.loads(artifacts[reference])
    mutate(document)
    artifacts[reference] = _json_bytes(document)
    return _repin_manifest(manifest, artifacts), artifacts


def _mutated_publication_bytes(raw: bytes) -> tuple[bytes, dict[str, bytes]]:
    """Return the fixture with raw publication-content bytes pinned exactly."""
    manifest, artifacts, _ = _fixture()
    artifacts[PUBLICATION_REF] = raw
    return _repin_manifest(manifest, artifacts), artifacts


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            lambda payload: payload.update(covered_from="2024-02-10", covered_through="2024-02-09"),
            "calendar covered_from follows covered_through",
        ),
        (
            lambda payload: payload.update(session_dates=list(reversed(payload["session_dates"]))),
            "calendar session_dates must be sorted and unique",
        ),
        (
            lambda payload: payload.update(
                session_dates=["2024-02-05", "2024-02-05", "2024-02-07"]
            ),
            "calendar session_dates must be sorted and unique",
        ),
        (
            lambda payload: payload.update(covered_from="2024-02-06"),
            "calendar session falls outside its declared bounds",
        ),
    ],
)
def test_calendar_parser_rejects_invalid_bounds_and_session_order(
    change: Callable[[dict[str, object]], None],
    message: str,
) -> None:
    """Pinned market sessions must stay ordered, unique, and inside declared coverage."""
    manifest, artifacts = _mutated_artifact(
        CALENDAR_REF,
        lambda document: change(document["payload"]),
    )

    with pytest.raises(StockDailyGapEvidenceError, match=message):
        load_stock_daily_gap_evidence_bundle(manifest, artifacts=artifacts)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            lambda payload: payload.update(calendar_ref="calendar:other:v1"),
            "expectation calendar_ref differs from manifest binding",
        ),
        (
            lambda payload: payload.update(covered_from="2024-02-10", covered_through="2024-02-09"),
            "expectation covered_from follows covered_through",
        ),
        (
            lambda payload: payload.update(expected_business_dates=["2024-02-05", "2024-02-05"]),
            "expectation expected dates must be sorted/unique",
        ),
        (
            lambda payload: payload.update(expected_business_dates=["2024-02-06", "2024-02-05"]),
            "expectation expected dates must be sorted/unique",
        ),
        (
            lambda payload: payload.update(expected_business_dates=["2024-02-04"]),
            "expectation expected date is outside its bounds",
        ),
        (
            lambda payload: payload.update(unresolved_observation_dates=["2024-02-05"]),
            "expected and unresolved dates overlap",
        ),
    ],
)
def test_expectation_parser_rejects_unbound_or_ambiguous_dates(
    change: Callable[[dict[str, object]], None],
    message: str,
) -> None:
    """Per-security expected dates must exactly follow their pinned calendar contract."""
    manifest, artifacts = _mutated_artifact(
        EXPECTATION_REF,
        lambda document: change(document["payload"]),
    )

    with pytest.raises(StockDailyGapEvidenceError, match=message):
        load_stock_daily_gap_evidence_bundle(manifest, artifacts=artifacts)


def test_retention_boundary_rejects_an_impossible_business_date() -> None:
    """An invalid retention boundary cannot shift the source's claimed history."""
    manifest, artifacts, _ = _fixture(include_retention=True)
    reference = "retention:600519:v1"
    retention = json.loads(artifacts[reference])
    retention["payload"]["retained_from"] = "2024-02-30"
    artifacts[reference] = _json_bytes(retention)
    manifest = _repin_manifest(manifest, artifacts)

    with pytest.raises(StockDailyGapEvidenceError, match="must use YYYY-MM-DD"):
        load_stock_daily_gap_evidence_bundle(manifest, artifacts=artifacts)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"[]", "root must be an object"),
        (b'{"rows":[{"trade_date":"2024-02-05","close":NaN}]}', "strict UTF-8 JSON"),
        (b"\xff", "strict UTF-8 JSON"),
        (b'{"rows":{}}', "daily_publication_content.rows must be an array"),
        (b'{"rows":[null]}', r"rows\[0\] must be an object"),
        (b'{"rows":[{}]}', r"rows\[0\].trade_date is required"),
        (
            b'{"rows":[{"trade_date":"2024-02-30"}]}',
            r"rows\[0\].trade_date must use YYYY-MM-DD",
        ),
        (
            b'{"rows":[{"trade_date":"2024-02-05","instrument_id":"CN:000001"}]}',
            "publication content row instrument_id differs from its metadata envelope",
        ),
    ],
)
def test_publication_content_parser_rejects_malformed_or_misbound_rows(
    raw: bytes,
    message: str,
) -> None:
    """The byte-pinned row stream must be strict JSON with canonical scoped dates."""
    manifest, artifacts = _mutated_publication_bytes(raw)

    with pytest.raises(StockDailyGapEvidenceError, match=message):
        load_stock_daily_gap_evidence_bundle(manifest, artifacts=artifacts)


def test_publication_date_rejects_supported_noncanonical_iso_spelling() -> None:
    """When the runtime parses ISO week dates, they still cannot replace YYYY-MM-DD."""
    alternate = "2024-W06-1"
    try:
        parsed = date.fromisoformat(alternate)
    except ValueError:
        pytest.skip("this Python version accepts only the canonical date spelling")
    assert parsed.isoformat() == "2024-02-05"
    raw = _json_bytes({"rows": [{"trade_date": alternate}]})
    manifest, artifacts = _mutated_publication_bytes(raw)

    with pytest.raises(
        StockDailyGapEvidenceError,
        match="rows\\[0\\].trade_date must use canonical YYYY-MM-DD",
    ):
        load_stock_daily_gap_evidence_bundle(manifest, artifacts=artifacts)


def test_publication_content_row_limit_is_checked_before_row_parsing() -> None:
    """Oversized publication arrays fail at the documented per-series cap."""
    raw = _json_bytes({"rows": [None] * (MAX_DATE_ENTRIES_PER_SERIES + 1)})
    manifest, artifacts = _mutated_publication_bytes(raw)

    with pytest.raises(StockDailyGapEvidenceError, match="per-series date limit"):
        load_stock_daily_gap_evidence_bundle(manifest, artifacts=artifacts)


def test_expectation_date_array_type_and_entry_limit_are_enforced_before_parsing() -> None:
    """Expectation inputs cannot bypass shape and work limits with JSON values."""
    manifest, artifacts = _mutated_artifact(
        EXPECTATION_REF,
        lambda document: document["payload"].update(expected_business_dates=None),
    )
    with pytest.raises(
        StockDailyGapEvidenceError, match="expected_business_dates must be an array"
    ):
        load_stock_daily_gap_evidence_bundle(manifest, artifacts=artifacts)

    manifest, artifacts = _mutated_artifact(
        EXPECTATION_REF,
        lambda document: document["payload"].update(
            expected_business_dates=[None] * (MAX_DATE_ENTRIES_PER_SERIES + 1)
        ),
    )
    with pytest.raises(StockDailyGapEvidenceError, match="exceeds the per-series date limit"):
        load_stock_daily_gap_evidence_bundle(manifest, artifacts=artifacts)


def test_calendar_identifier_is_rejected_when_empty_or_whitespace_padded() -> None:
    """Calendar identity labels are strict identifiers, not normalized input."""
    manifest, artifacts = _mutated_artifact(
        CALENDAR_REF,
        lambda document: document["payload"].update(calendar_id=" SSE "),
    )

    with pytest.raises(
        StockDailyGapEvidenceError, match="calendar_id must be a bounded nonempty string"
    ):
        load_stock_daily_gap_evidence_bundle(manifest, artifacts=artifacts)


def test_missing_registry_boundary_artifact_is_not_silently_ignored() -> None:
    """A referenced lifecycle status without its exact descriptor fails closed."""
    manifest, artifacts, _ = _fixture()
    value = json.loads(manifest)
    status_ref = value["series"][0]["lifecycle_ref"]
    artifacts.pop(status_ref)
    value["artifacts"] = [item for item in value["artifacts"] if item["evidence_ref"] != status_ref]

    with pytest.raises(StockDailyGapEvidenceError, match="required evidence artifact is missing"):
        load_stock_daily_gap_evidence_bundle(_json_bytes(value), artifacts=artifacts)


def test_lifecycle_status_boolean_field_rejects_integer_lookalikes() -> None:
    """JSON true/false are required for the lifecycle uncertainty indicator."""
    manifest, artifacts = _mutated_artifact(
        "status:600519:v1",
        lambda document: document["payload"].update(start_boundary_unknown=0),
    )

    with pytest.raises(StockDailyGapEvidenceError, match="must be a JSON boolean"):
        load_stock_daily_gap_evidence_bundle(manifest, artifacts=artifacts)


def test_alias_payload_must_match_the_registry_alias_identity() -> None:
    """Re-pinning altered alias bytes cannot change the reviewed ticker mapping."""
    manifest, artifacts, _ = _fixture(with_alias=True)
    alias_ref = "alias:600519:v1"
    alias = json.loads(artifacts[alias_ref])
    alias["payload"]["alias"] = "600519.SZ"
    artifacts[alias_ref] = _json_bytes(alias)
    manifest = _repin_manifest(manifest, artifacts)

    with pytest.raises(
        StockDailyGapEvidenceError, match="alias evidence differs from registry alias"
    ):
        load_stock_daily_gap_evidence_bundle(manifest, artifacts=artifacts)
