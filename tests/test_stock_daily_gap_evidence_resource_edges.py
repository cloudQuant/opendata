"""Strict parsing and bounded-resource edge tests for offline evidence bundles."""

from __future__ import annotations

import hashlib
import json
from datetime import date

import pytest

from opendata.pipeline import stock_daily_gap_evidence_bundle as evidence_bundle
from opendata.pipeline.stock_daily_gap_evidence_bundle import (
    StockDailyGapEvidenceError,
    load_stock_daily_gap_evidence_bundle,
)


def _manifest(artifact_descriptors: object) -> bytes:
    """Build the smallest manifest needed to exercise descriptor validation."""
    return json.dumps(
        {
            "schema_version": 1,
            "registry_ref": "registry:test:v1",
            "artifacts": artifact_descriptors,
            "series": [],
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _descriptor(reference: str, raw: bytes) -> dict[str, str]:
    """Pin a small non-registry artifact with its exact bytes."""
    return {
        "evidence_ref": reference,
        "kind": "instrument_status",
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


@pytest.mark.parametrize(
    "raw",
    [
        b"{",
        b"\xff",
        b'{"nested":{"key":1,"key":2}}',
        b'{"value":NaN}',
        b'{"value":Infinity}',
        b'{"value":-Infinity}',
    ],
)
def test_manifest_parser_rejects_ambiguous_or_invalid_json(raw: bytes) -> None:
    """Malformed UTF-8, duplicate keys, and non-standard numbers fail closed."""
    with pytest.raises(StockDailyGapEvidenceError, match="strict UTF-8 JSON"):
        load_stock_daily_gap_evidence_bundle(raw, artifacts={})


@pytest.mark.parametrize("raw", [b"[]", b"null", b'"manifest"'])
def test_manifest_parser_requires_an_object_root(raw: bytes) -> None:
    """A syntactically valid JSON scalar or array is not a manifest object."""
    with pytest.raises(StockDailyGapEvidenceError, match="root must be an object"):
        load_stock_daily_gap_evidence_bundle(raw, artifacts={})


@pytest.mark.parametrize("descriptors", [None, {}, "not-an-array"])
def test_artifact_descriptor_parser_requires_an_array(descriptors: object) -> None:
    """The manifest descriptor collection must have the documented array shape."""
    with pytest.raises(StockDailyGapEvidenceError, match="manifest.artifacts must be an array"):
        load_stock_daily_gap_evidence_bundle(_manifest(descriptors), artifacts={})


@pytest.mark.parametrize("descriptor", [None, "descriptor", 7, []])
def test_artifact_descriptor_parser_rejects_non_object_entries(descriptor: object) -> None:
    """Scalar descriptor entries cannot be silently ignored during pin validation."""
    message = r"manifest.artifacts\[0\] must be an object"
    with pytest.raises(StockDailyGapEvidenceError, match=message):
        load_stock_daily_gap_evidence_bundle(_manifest([descriptor]), artifacts={})


def test_manifest_byte_limit_rejects_oversized_input_before_json_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The manifest cap is enforced on raw bytes before decoding attacker input."""
    monkeypatch.setattr(evidence_bundle, "MAX_MANIFEST_BYTES", 1)

    with pytest.raises(StockDailyGapEvidenceError, match="manifest exceeds the byte limit"):
        load_stock_daily_gap_evidence_bundle(b"{}", artifacts={})


def test_artifact_count_limit_is_enforced_before_byte_map_comparison(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A descriptor bomb is stopped before checking or loading its byte mapping."""
    monkeypatch.setattr(evidence_bundle, "MAX_ARTIFACTS", 1)
    descriptors = [
        {"evidence_ref": "artifact:one", "kind": "instrument_status", "sha256": "0" * 64},
        {"evidence_ref": "artifact:two", "kind": "instrument_status", "sha256": "1" * 64},
    ]

    with pytest.raises(StockDailyGapEvidenceError, match="artifacts exceeds 1 entries"):
        load_stock_daily_gap_evidence_bundle(_manifest(descriptors), artifacts={})


@pytest.mark.parametrize(
    ("individual_limit", "total_limit", "expected_message"),
    [
        (1, 100, "artifact 'artifact:one' exceeds the byte limit"),
        (100, 1, "total artifact bytes exceed the bundle limit"),
    ],
)
def test_artifact_byte_budgets_reject_before_json_parsing(
    monkeypatch: pytest.MonkeyPatch,
    individual_limit: int,
    total_limit: int,
    expected_message: str,
) -> None:
    """Per-artifact and aggregate byte caps both apply to the exact input bytes."""
    raw = b"{}"
    monkeypatch.setattr(evidence_bundle, "MAX_ARTIFACT_BYTES", individual_limit)
    monkeypatch.setattr(evidence_bundle, "MAX_TOTAL_ARTIFACT_BYTES", total_limit)

    with pytest.raises(StockDailyGapEvidenceError, match=expected_message):
        load_stock_daily_gap_evidence_bundle(
            _manifest([_descriptor("artifact:one", raw)]),
            artifacts={"artifact:one": raw},
        )


def test_manifest_and_artifact_limits_accept_exact_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Limits are inclusive at the configured byte count, not one byte below it."""
    raw = b"{}"
    manifest = _manifest([_descriptor("artifact:one", raw)])
    monkeypatch.setattr(evidence_bundle, "MAX_MANIFEST_BYTES", len(manifest))
    monkeypatch.setattr(evidence_bundle, "MAX_ARTIFACT_BYTES", len(raw))
    monkeypatch.setattr(evidence_bundle, "MAX_TOTAL_ARTIFACT_BYTES", len(raw))

    # The empty artifact proceeds to schema validation, proving all three byte
    # bounds accept equality and that the later failure is semantic.
    with pytest.raises(StockDailyGapEvidenceError, match="missing or unsupported fields"):
        load_stock_daily_gap_evidence_bundle(manifest, artifacts={"artifact:one": raw})


def test_per_series_date_limit_is_inclusive_and_rejects_one_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both supported date-array readers enforce the same per-series bound."""
    monkeypatch.setattr(evidence_bundle, "MAX_DATE_ENTRIES_PER_SERIES", 2)
    two_dates = ["2024-01-02", "2024-01-03"]
    three_rows = {"rows": [{"trade_date": value} for value in [*two_dates, "2024-01-04"]]}

    assert evidence_bundle._date_tuple(two_dates, "dates") == (
        date(2024, 1, 2),
        date(2024, 1, 3),
    )
    with pytest.raises(StockDailyGapEvidenceError, match="per-series date limit"):
        evidence_bundle._date_tuple([*two_dates, "2024-01-04"], "dates")

    accepted = evidence_bundle._read_publication_content(
        {"rows": [{"trade_date": value} for value in two_dates]}
    )
    assert accepted == (date(2024, 1, 2), date(2024, 1, 3))
    with pytest.raises(StockDailyGapEvidenceError, match="per-series date limit"):
        evidence_bundle._read_publication_content(three_rows)
