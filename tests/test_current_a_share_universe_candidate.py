"""Synthetic offline tests for the current A-share universe candidate builder."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

import opendata.pipeline.current_a_share_universe_candidate as candidate_module
from opendata.pipeline.current_a_share_universe_candidate import (
    MANIFEST_SCHEMA,
    UniverseCandidateError,
    build_universe_candidate,
    load_manifest,
    render_snapshot,
)

_AS_OF = "2026-10-31T00:00:00Z"


def _write_json(path: Path, payload: object) -> str:
    """Write compact UTF-8 JSON and return its SHA-256."""
    path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rows_source(
    tmp_path: Path,
    name: str,
    rows: list[dict[str, object]],
    *,
    source_id: str = "test.rows",
    market: str = "CN-SSE",
    mapping: dict[str, str] | None = None,
    page: dict[str, object] | None = None,
) -> dict[str, object]:
    """Create one normalized row-list source entry."""
    path = tmp_path / f"{name}.json"
    digest = _write_json(path, {"rows": rows})
    entry: dict[str, object] = {
        "source_id": source_id,
        "capture_id": f"capture-{name}",
        "market": market,
        "as_of": _AS_OF,
        "format": "normalized_rows_json",
        "path": str(path),
        "sha256": digest,
        "mapping": mapping or {"symbol_field": "code", "name_field": "name"},
    }
    if page is not None:
        entry["page"] = page
    return entry


def _manifest(tmp_path: Path, entries: list[dict[str, object]]) -> Path:
    """Write a manifest around explicit source entries."""
    path = tmp_path / "manifest.json"
    _write_json(
        path,
        {
            "schema": MANIFEST_SCHEMA,
            "as_of": _AS_OF,
            "sources": entries,
        },
    )
    return path


def _build(manifest_path: Path) -> dict[str, object]:
    """Load one manifest and return the rendered snapshot object."""
    manifest, digest = load_manifest(manifest_path)
    return json.loads(render_snapshot(build_universe_candidate(manifest, manifest_sha256=digest)))


def test_duplicate_agreement_across_sources_is_reported_not_merged(tmp_path: Path) -> None:
    rows = [{"code": "600000", "name": "浦发银行"}]
    manifest = _manifest(
        tmp_path,
        [
            _rows_source(tmp_path, "a", rows, source_id="source.a"),
            _rows_source(tmp_path, "b", rows, source_id="source.b"),
        ],
    )
    snapshot = _build(manifest)
    assert snapshot["counts"]["candidates_total"] == 2
    assert snapshot["counts"]["candidate_identities_total"] == 1
    assert len(snapshot["candidates"]) == 1
    candidate = snapshot["candidates"][0]
    assert candidate["symbol"] == "600000"
    assert candidate["record_count"] == 2
    assert candidate["source_ids"] == ["source.a", "source.b"]
    assert len(snapshot["duplicates"]) == 1
    duplicate = snapshot["duplicates"][0]
    assert duplicate["agreement"] is True
    assert duplicate["source_count"] == 2
    assert snapshot["conflicts"] == []


def test_cross_source_conflict_is_retained_without_guessing(tmp_path: Path) -> None:
    manifest = _manifest(
        tmp_path,
        [
            _rows_source(
                tmp_path, "a", [{"code": "600000", "name": "浦发银行"}], source_id="source.a"
            ),
            _rows_source(
                tmp_path, "b", [{"code": "600000", "name": "不同名称"}], source_id="source.b"
            ),
        ],
    )
    snapshot = _build(manifest)
    assert snapshot["candidates"] == []
    assert snapshot["counts"]["conflicts_total"] == 2
    assert len(snapshot["conflicts"]) == 1
    conflict = snapshot["conflicts"][0]
    assert conflict["kind"] == "cross_source_display_name_disagreement"
    assert conflict["symbol"] == "600000"
    assert {record["disposition"] for record in conflict["records"]} == {"conflict"}
    assert {record["reason"] for record in snapshot["records"]} == {"display_name_disagreement"}


def test_same_source_duplicate_agreement_is_a_duplicate_only(tmp_path: Path) -> None:
    manifest = _manifest(
        tmp_path,
        [
            _rows_source(
                tmp_path,
                "a",
                [{"code": "600000", "name": "浦发银行"}, {"code": "600000", "name": "浦发银行"}],
            )
        ],
    )
    snapshot = _build(manifest)
    assert snapshot["counts"]["candidates_total"] == 2
    assert len(snapshot["candidates"]) == 1
    assert snapshot["duplicates"][0]["agreement"] is True
    assert snapshot["conflicts"] == []


def test_bse_codes_are_never_merged_into_shanghai_or_shenzhen(tmp_path: Path) -> None:
    manifest = _manifest(
        tmp_path,
        [
            _rows_source(
                tmp_path,
                "bse",
                [{"code": "920001", "name": "北交所样本"}],
                source_id="source.bse",
                market="CN-BSE",
            ),
            _rows_source(
                tmp_path,
                "szse",
                [
                    {"code": "830001", "name": "错误归属"},
                    {"code": "920001", "name": "错误归属"},
                ],
                source_id="source.szse",
                market="CN-SZSE",
            ),
            _rows_source(
                tmp_path,
                "sse",
                [{"code": "430047", "name": "错误归属"}],
                source_id="source.sse",
                market="CN-SSE",
            ),
        ],
    )
    snapshot = _build(manifest)
    assert [(item["market"], item["symbol"]) for item in snapshot["candidates"]] == [
        ("CN-BSE", "920001")
    ]
    assert snapshot["bse_supported"] == "true"
    conflict_records = [
        record for record in snapshot["records"] if record["disposition"] == "conflict"
    ]
    assert {record["reason"] for record in conflict_records} == {
        "bse_reserved_prefix_outside_declared_market"
    }
    assert snapshot["counts"]["by_market"]["CN-SZSE"]["conflicts"] == 2
    assert snapshot["counts"]["by_market"]["CN-SSE"]["conflicts"] == 1
    assert snapshot["counts"]["by_market"]["CN-BSE"]["candidates"] == 1


def test_missing_pagination_evidence_stays_unknown(tmp_path: Path) -> None:
    manifest = _manifest(
        tmp_path,
        [_rows_source(tmp_path, "a", [{"code": "600000", "name": "浦发银行"}])],
    )
    snapshot = _build(manifest)
    assert snapshot["pagination"]["status"] == "UNKNOWN"
    assert snapshot["pagination"]["attested"] is False
    assert snapshot["pagination"]["sources"][0]["reasons"] == ["no_pagination_evidence"]
    assert snapshot["unknowns"]["pagination_termination"] == "UNKNOWN"
    assert snapshot["unknowns"]["retention"] == "UNKNOWN"


def test_page_sequence_gap_stays_unknown(tmp_path: Path) -> None:
    manifest = _manifest(
        tmp_path,
        [
            _rows_source(
                tmp_path,
                "a",
                [{"code": "600000", "name": "浦发银行"}],
                page={"page_sequence": 2, "terminal": True},
            )
        ],
    )
    snapshot = _build(manifest)
    assert snapshot["pagination"]["status"] == "UNKNOWN"
    reasons = snapshot["pagination"]["sources"][0]["reasons"]
    assert "page_sequence_gap" in reasons
    assert "reported_total_missing" in reasons


def test_declared_page_chain_is_consistent_but_not_attested(tmp_path: Path) -> None:
    manifest = _manifest(
        tmp_path,
        [
            _rows_source(
                tmp_path,
                "a",
                [{"code": "600000", "name": "浦发银行"}],
                page={
                    "page_sequence": 1,
                    "terminal": True,
                    "reported_total": 1,
                    "reported_total_basis": "row_count",
                    "declared_row_count": 1,
                },
            )
        ],
    )
    snapshot = _build(manifest)
    assert snapshot["pagination"]["status"] == "DECLARED_CONSISTENT"
    assert snapshot["pagination"]["attested"] is False
    assert snapshot["unknowns"]["pagination_termination"] == "UNKNOWN"


def test_truncated_declared_page_evidence_stays_unknown(tmp_path: Path) -> None:
    manifest = _manifest(
        tmp_path,
        [
            _rows_source(
                tmp_path,
                "a",
                [{"code": "600000", "name": "浦发银行"}],
                page={
                    "page_sequence": 1,
                    "terminal": False,
                    "reported_total": 2,
                    "reported_total_basis": "row_count",
                    "declared_row_count": 1,
                },
            )
        ],
    )
    snapshot = _build(manifest)
    assert snapshot["pagination"]["status"] == "UNKNOWN"
    reasons = snapshot["pagination"]["sources"][0]["reasons"]
    assert "final_page_not_terminal" in reasons
    assert "row_count_mismatch" in reasons


def test_symlinked_source_is_rejected(tmp_path: Path) -> None:
    real = tmp_path / "real.json"
    _write_json(real, {"rows": [{"code": "600000", "name": "浦发银行"}]})
    link = tmp_path / "link.json"
    os.symlink(real, link)
    manifest = _manifest(
        tmp_path,
        [
            {
                "source_id": "test.rows",
                "capture_id": "capture-link",
                "market": "CN-SSE",
                "as_of": _AS_OF,
                "format": "normalized_rows_json",
                "path": str(link),
                "sha256": hashlib.sha256(real.read_bytes()).hexdigest(),
                "mapping": {"symbol_field": "code"},
            }
        ],
    )
    with pytest.raises(UniverseCandidateError) as caught:
        _build(manifest)
    assert caught.value.code == "path_symlink_rejected"


def test_symlinked_manifest_is_rejected(tmp_path: Path) -> None:
    manifest = _manifest(
        tmp_path,
        [_rows_source(tmp_path, "a", [{"code": "600000", "name": "浦发银行"}])],
    )
    link = tmp_path / "manifest-link.json"
    os.symlink(manifest, link)
    with pytest.raises(UniverseCandidateError) as caught:
        load_manifest(link)
    assert caught.value.code == "path_symlink_rejected"


def test_oversized_source_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _manifest(
        tmp_path,
        [_rows_source(tmp_path, "a", [{"code": "600000", "name": "浦发银行"}])],
    )
    monkeypatch.setattr(candidate_module, "MAX_SOURCE_BYTES", 8)
    with pytest.raises(UniverseCandidateError) as caught:
        _build(manifest)
    assert caught.value.code == "path_oversize"


def test_oversized_manifest_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _manifest(
        tmp_path,
        [_rows_source(tmp_path, "a", [{"code": "600000", "name": "浦发银行"}])],
    )
    monkeypatch.setattr(candidate_module, "MAX_MANIFEST_BYTES", 8)
    with pytest.raises(UniverseCandidateError) as caught:
        load_manifest(manifest)
    assert caught.value.code == "path_oversize"


def test_deterministic_output(tmp_path: Path) -> None:
    manifest = _manifest(
        tmp_path,
        [
            _rows_source(
                tmp_path,
                "a",
                [
                    {"code": "600000", "name": "浦发银行"},
                    {"code": "000001", "name": "平安银行"},
                ],
            )
        ],
    )
    parsed, digest = load_manifest(manifest)
    first = render_snapshot(build_universe_candidate(parsed, manifest_sha256=digest))
    second = render_snapshot(build_universe_candidate(parsed, manifest_sha256=digest))
    assert first == second
    assert first.endswith("\n")


def test_explicit_non_attestation_fields(tmp_path: Path) -> None:
    manifest = _manifest(
        tmp_path,
        [_rows_source(tmp_path, "a", [{"code": "600000", "name": "浦发银行"}])],
    )
    snapshot = _build(manifest)
    assert snapshot["universe_census_attested"] is False
    assert snapshot["delisted_lifecycle_included"] is False
    assert snapshot["authoritative"] is False
    assert snapshot["publication"] == "NOT_PERFORMED"
    assert snapshot["database_or_api_published"] is False
    assert snapshot["bse_supported"] == "unknown"
    assert snapshot["unknowns"] == {
        "pagination_termination": "UNKNOWN",
        "retention": "UNKNOWN",
        "market_completeness": "UNKNOWN",
        "source_authenticity": "UNKNOWN",
        "delisted_lifecycle": "NOT_INCLUDED",
        "identity_review": "REQUIRED",
    }


def test_hash_mismatch_is_rejected(tmp_path: Path) -> None:
    entry = _rows_source(tmp_path, "a", [{"code": "600000", "name": "浦发银行"}])
    entry["sha256"] = "0" * 64
    manifest = _manifest(tmp_path, [entry])
    with pytest.raises(UniverseCandidateError) as caught:
        _build(manifest)
    assert caught.value.code == "source_sha256_mismatch"


def test_unknown_and_unsupported_rows_are_explicit(tmp_path: Path) -> None:
    manifest = _manifest(
        tmp_path,
        [
            _rows_source(
                tmp_path,
                "a",
                [
                    {"code": "ABCDEF", "name": "无效代码"},
                    {"code": "600001", "name": "基金样本", "asset": "fund"},
                ],
                mapping={
                    "symbol_field": "code",
                    "name_field": "name",
                    "asset_type_field": "asset",
                    "asset_type_value": "a-share",
                },
            )
        ],
    )
    snapshot = _build(manifest)
    dispositions = {(record["reason"], record["disposition"]) for record in snapshot["records"]}
    assert ("source_code_unrecognized", "unknown") in dispositions
    assert ("asset_type_not_supported", "unsupported") in dispositions
    assert snapshot["counts"]["unknown_total"] == 1
    assert snapshot["counts"]["unsupported_total"] == 1


def test_source_native_code_forms_and_exchange_conflicts(tmp_path: Path) -> None:
    manifest = _manifest(
        tmp_path,
        [
            _rows_source(
                tmp_path,
                "a",
                [
                    {"code": "000001.SZ", "name": "平安银行"},
                    {"code": "SZ000002", "name": "万科"},
                    {"code": "000003.SH", "name": "错误后缀"},
                    {"code": "000004", "name": "错误字段", "exchange": "SH"},
                ],
                market="CN-SZSE",
                mapping={
                    "symbol_field": "code",
                    "name_field": "name",
                    "exchange_field": "exchange",
                },
            )
        ],
    )
    snapshot = _build(manifest)
    assert {item["symbol"] for item in snapshot["candidates"]} == {"000001", "000002"}
    reasons = {
        record["symbol"]: record["reason"]
        for record in snapshot["records"]
        if record["disposition"] == "conflict"
    }
    assert reasons == {
        "000003": "source_code_exchange_conflict",
        "000004": "source_exchange_field_conflict",
    }


def test_raw_sse_page_format(tmp_path: Path) -> None:
    body = {
        "result": [{"COMPANY_CODE": "600000", "SEC_NAME_CN": "浦发银行", "EXCHANGE": "SH"}],
        "pageHelp": {"pageCount": 1, "pageNo": "1"},
    }
    path = tmp_path / "sse-page.json"
    digest = _write_json(path, body)
    manifest = _manifest(
        tmp_path,
        [
            {
                "source_id": "sse.instrument",
                "capture_id": "sse-capture",
                "market": "CN-SSE",
                "as_of": _AS_OF,
                "format": "sse_instrument_pages_json",
                "path": str(path),
                "sha256": digest,
                "mapping": {
                    "symbol_field": "COMPANY_CODE",
                    "name_field": "SEC_NAME_CN",
                    "exchange_field": "EXCHANGE",
                },
                "page": {"page_sequence": 1, "selector": "current_main_a"},
            }
        ],
    )
    snapshot = _build(manifest)
    assert snapshot["candidates"] == [
        {
            "market": "CN-SSE",
            "symbol": "600000",
            "display_name": "浦发银行",
            "record_count": 1,
            "source_ids": ["sse.instrument"],
            "capture_ids": ["sse-capture"],
        }
    ]
    assert snapshot["records"][0]["source_context"] == "current_main_a"
    assert snapshot["records"][0]["source_page_number"] == "1"


def test_raw_sse_page_gap_keeps_rows_and_marks_pagination_unknown(tmp_path: Path) -> None:
    entries = []
    for name, sequence, code, terminal, cursor, next_cursor in (
        ("page1", 1, "600000", False, None, "c2"),
        ("page3", 3, "600001", True, "c2", None),
    ):
        path = tmp_path / f"{name}.json"
        digest = _write_json(
            path,
            {
                "result": [{"COMPANY_CODE": code, "SEC_NAME_CN": "样本"}],
                "pageHelp": {"pageCount": 3, "pageNo": str(sequence)},
            },
        )
        entries.append(
            {
                "source_id": "sse.instrument",
                "capture_id": "sse-gapped-capture",
                "market": "CN-SSE",
                "as_of": _AS_OF,
                "format": "sse_instrument_pages_json",
                "path": str(path),
                "sha256": digest,
                "mapping": {"symbol_field": "COMPANY_CODE", "name_field": "SEC_NAME_CN"},
                "page": {
                    "page_sequence": sequence,
                    "selector": "current_main_a",
                    "terminal": terminal,
                    "request_cursor": cursor,
                    "next_cursor": next_cursor,
                },
            }
        )
    snapshot = _build(_manifest(tmp_path, entries))
    assert {item["symbol"] for item in snapshot["candidates"]} == {"600000", "600001"}
    assert snapshot["pagination"]["status"] == "UNKNOWN"
    assert "page_sequence_gap" in snapshot["pagination"]["sources"][0]["reasons"]


def test_raw_fuyao_page_format(tmp_path: Path) -> None:
    body = {
        "code": 0,
        "data": {
            "item": [
                {
                    "thscode": "000001.SZ",
                    "name": "平安银行",
                    "exchange": "SZ",
                    "asset_type": "a-share",
                }
            ]
        },
    }
    path = tmp_path / "fuyao-page.json"
    digest = _write_json(path, body)
    manifest = _manifest(
        tmp_path,
        [
            {
                "source_id": "fuyao.instrument",
                "capture_id": "fuyao-capture",
                "market": "CN-SZSE",
                "as_of": _AS_OF,
                "format": "fuyao_instrument_pages_json",
                "path": str(path),
                "sha256": digest,
                "mapping": {},
                "page": {"page_sequence": 1},
            }
        ],
    )
    snapshot = _build(manifest)
    assert snapshot["candidates"][0]["symbol"] == "000001"
    assert snapshot["records"][0]["source_context"] == "fuyao_a_share_catalog"


@pytest.mark.skipif(
    os.environ.get("OPENDATA_AFI204_REAL_CAPTURE") != "1",
    reason="opt-in real captured SZSE workbook replay",
)
def test_real_szse_current_a_workbook_replay(tmp_path: Path) -> None:
    """Replay the already captured SZSE current-A workbook without new semantics."""
    capture = (
        Path(__file__).resolve().parents[1]
        / "var/reports/afi204/szse-official-xlsx-capture-20261030-5809685a"
        / "szse-current-a-share-list.xlsx"
    )
    if not capture.is_file():
        pytest.skip("real SZSE capture is not present")
    manifest = _manifest(
        tmp_path,
        [
            {
                "source_id": "szse.current_a_xlsx",
                "capture_id": "szse-official-xlsx-20261030-5809685a",
                "market": "CN-SZSE",
                "as_of": "2026-10-30T12:13:34Z",
                "format": "szse_current_a_xlsx",
                "path": str(capture),
                "sha256": "c466df025f3d855ebb23b0a9c9a02da38d411a97936dea36775c1c01e74f0bc7",
                "mapping": {"symbol_column": "E", "name_column": "F"},
            }
        ],
    )
    snapshot = _build(manifest)
    assert snapshot["counts"]["records_total"] == 2905
    assert snapshot["counts"]["candidates_total"] == 2905
    assert snapshot["counts"]["candidate_identities_total"] == 2905
    assert snapshot["counts"]["conflicts_total"] == 0
    assert snapshot["pagination"]["status"] == "UNKNOWN"
    assert snapshot["pagination"]["sources"][0]["reasons"] == ["no_pagination_evidence"]
    assert snapshot["bse_supported"] == "unknown"
    assert snapshot["universe_census_attested"] is False
    assert snapshot["delisted_lifecycle_included"] is False
    assert {record["market"] for record in snapshot["records"]} == {"CN-SZSE"}
