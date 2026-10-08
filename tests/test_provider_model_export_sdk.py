"""Offline contract tests for the SDK's bounded native export method."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import stat
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest

from opendata.data.models import BlsFootnote, BlsObservation, EquityHistorical, SeriesObservation
from opendata_client import (
    AuthenticationError,
    InvalidQueryError,
    NotFoundError,
    OpendataClient,
    OpendataClientError,
    PermissionDeniedError,
    UnsupportedQueryError,
)

_CREATED_AT = "2026-10-08T00:00:00Z"
_CONSISTENCY = "single_repeatable_read_transaction"
_COMPLETENESS = "NOT_ASSESSED"


def _fred_rows() -> list[dict[str, Any]]:
    common = {
        "series_id": "CPIAUCSL",
        "date": date(2024, 2, 1),
        "realtime_end": date(2024, 12, 31),
        "transform_units": "lin",
        "output_type": 1,
        "requested_frequency": None,
        "requested_aggregation_method": "avg",
    }
    return [
        SeriesObservation(
            **common,
            value=None,
            realtime_start=date(2024, 1, 1),
        ).model_dump(mode="json"),
        SeriesObservation(
            **common,
            value=3.1,
            realtime_start=date(2024, 2, 15),
        ).model_dump(mode="json"),
    ]


def _bls_rows() -> list[dict[str, Any]]:
    return [
        BlsObservation(
            series_id="LNS14000000",
            year=2025,
            period="M13",
            period_name="Annual average",
            value=4.1,
            footnotes=(BlsFootnote(code="P", text="Preliminary."), BlsFootnote()),
            latest=None,
            preliminary=True,
            api_version="v2",
        ).model_dump(mode="json")
    ]


def _fmp_rows() -> list[dict[str, Any]]:
    common = {
        "symbol": "AAPL",
        "date": date(2024, 1, 2),
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.5,
        "change": 0.5,
        "change_percent": 0.5,
        "vwap": 100.25,
        "currency": None,
        "currency_semantics": "source_unverified",
        "volume_unit": None,
        "volume_unit_semantics": "source_unverified",
        "query_window_scope": "explicit",
        "window_boundary_semantics": "source_unverified",
        "provider_default_window_semantics": "source_unverified",
        "close_adjustment_semantics": "split_adjusted_per_source_faq",
        "adj_close_provided": False,
    }
    return [
        EquityHistorical(**common, volume=2**256 + 17).model_dump(mode="json"),
        EquityHistorical(**common, volume=-0.0).model_dump(mode="json"),
    ]


def _metadata(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "kind": "metadata",
        "schema_version": 1,
        "source": "fred",
        "model": "FredSeries",
        "domain": "fred_series",
        "verified": False,
        "created_at": _CREATED_AT,
        "consistency": _CONSISTENCY,
        "completeness": _COMPLETENESS,
    }
    record.update(overrides)
    return record


def _json_line(value: Any) -> bytes:
    return json.dumps(
        value,
        allow_nan=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _snapshot(
    rows: list[dict[str, Any]] | None = None,
    *,
    source: str = "fred",
    model: str = "FredSeries",
    domain: str = "fred_series",
    metadata_overrides: dict[str, Any] | None = None,
    summary_rows: int | None = None,
) -> bytes:
    export_rows = rows or []
    metadata_values: dict[str, Any] = {
        "source": source,
        "model": model,
        "domain": domain,
    }
    metadata_values.update(metadata_overrides or {})
    metadata = _metadata(**metadata_values)
    summary = {
        "kind": "summary",
        "rows": len(export_rows) if summary_rows is None else summary_rows,
        "snapshot_complete": True,
    }
    lines = [metadata, *({"kind": "row", "data": row} for row in export_rows), summary]
    return b"".join(_json_line(line) + b"\n" for line in lines)


def _snapshot_from_lines(lines: list[bytes]) -> bytes:
    return b"".join(line + b"\n" for line in lines)


def _deep_snapshot() -> bytes:
    nested: dict[str, Any] = {}
    for _ in range(258):
        nested = {"nested": nested}
    return _snapshot([{"value": nested}])


def _headers(body: bytes, row_count: int) -> dict[str, str]:
    return {
        "Content-Type": "application/x-ndjson",
        "Content-Length": str(len(body)),
        "X-Content-SHA256": hashlib.sha256(body).hexdigest(),
        "X-Row-Count": str(row_count),
        "X-NDJSON-Schema-Version": "1",
        "X-Snapshot-Complete": "true",
        "X-Snapshot-Consistency": _CONSISTENCY,
        "X-Completeness": _COMPLETENESS,
        "X-Export-Created-At": _CREATED_AT,
    }


class _ChunkStream(httpx.SyncByteStream):
    def __init__(self, chunks: list[bytes], error: Exception | None = None) -> None:
        self.chunks = chunks
        self.error = error
        self.yielded = 0
        self.close_count = 0

    def __iter__(self):
        for chunk in self.chunks:
            self.yielded += 1
            yield chunk
        if self.error is not None:
            raise self.error

    def close(self) -> None:
        self.close_count += 1


def _export_client(
    body: bytes,
    row_count: int,
    *,
    headers: dict[str, str] | None = None,
    chunks: list[bytes] | None = None,
    stream_error: Exception | None = None,
    status_code: int = 200,
    error_body: bytes = b"",
) -> tuple[OpendataClient, list[httpx.Request], list[_ChunkStream]]:
    seen: list[httpx.Request] = []
    streams: list[_ChunkStream] = []
    response_headers = _headers(body, row_count)
    if headers:
        response_headers.update(headers)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if status_code != 200:
            return httpx.Response(status_code, content=error_body, request=request)
        body_chunks = (
            chunks
            if chunks is not None
            else [body[index : index + 97] for index in range(0, len(body), 97)]
        )
        stream = _ChunkStream(body_chunks, stream_error)
        streams.append(stream)
        return httpx.Response(
            status_code,
            headers=response_headers,
            stream=stream,
            request=request,
        )

    client = OpendataClient(
        "http://api.test",
        api_key="od-export-test-key",
        transport=httpx.MockTransport(handler),
    )
    return client, seen, streams


def _default_query(**overrides: Any) -> dict[str, Any]:
    query: dict[str, Any] = {
        "filters": {"series_id": "CPIAUCSL"},
        "start": "2024-01-01",
        "end": "2024-12-31",
        "max_records": 10,
        "max_bytes": 2_000_000,
    }
    query.update(overrides)
    return query


def test_export_request_is_exact_and_writes_complete_fred_snapshot(tmp_path: Path) -> None:
    rows = _fred_rows()
    body = _snapshot(rows)
    client, requests, streams = _export_client(body, len(rows))
    destination = tmp_path / "fred.ndjson"
    query = _default_query()
    original_query = copy.deepcopy(query)

    result = client.export_provider_model(
        source="fred",
        model="FredSeries",
        query=query,
        destination=destination,
    )

    request = requests[0]
    assert request.method == "POST"
    assert request.url.path == "/api/v1/providers/fred/models/FredSeries/warehouse/export"
    assert request.url.query == b""
    assert request.headers["x-api-key"] == "od-export-test-key"
    assert request.headers["content-type"] == "application/json"
    assert request.content == json.dumps(
        {"query": original_query},
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    assert query == original_query
    assert destination.read_bytes() == body
    assert result["source"] == "fred"
    assert result["model"] == "FredSeries"
    assert result["verified"] is False
    assert result["row_count"] == 2
    assert result["byte_count"] == len(body)
    assert result["sha256"] == hashlib.sha256(body).hexdigest()
    assert result["snapshot_complete"] is True
    assert result["destination"] == str(destination)
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert streams and streams[0].yielded > 1 and streams[0].close_count == 1
    client.close()


def test_export_accepts_production_utc_timestamp_wire_formats(tmp_path: Path) -> None:
    metadata_time = "2026-10-08T01:00:00.123456Z"
    header_time = "2026-10-08T01:00:00.123456+00:00"
    body = _snapshot(metadata_overrides={"created_at": metadata_time})
    client, _, streams = _export_client(
        body,
        0,
        headers={"X-Export-Created-At": header_time},
    )
    destination = tmp_path / "production-time.ndjson"

    result = client.export_provider_model(
        source="fred",
        model="FredSeries",
        query={},
        destination=destination,
    )

    assert destination.read_bytes() == body
    assert result["created_at"] == metadata_time
    assert streams and streams[0].close_count == 1


@pytest.mark.parametrize(
    ("metadata_time", "header_time"),
    [
        ("2026-10-08T01:00:00.123456Z", "2026-10-08T01:00:00.123457+00:00"),
        ("2026-10-08T01:00:00.123456Z", "2026-10-08T01:00:00.123456001+00:00"),
        ("2026-10-08T01:00:00.123456001Z", "2026-10-08T01:00:00.123456+00:00"),
        ("2026-10-08T01:00:00.123456Z", "2026-10-09T01:00:00.123456+00:00"),
        ("2026-10-08T01:00:00.123456Z", "2026-10-08T02:00:00.123456+01:00"),
        ("2026-10-08T01:00:00.123456Z", "2026-10-08T01:00:00.123456"),
        ("2026-10-08T02:00:00.123456+01:00", "2026-10-08T01:00:00.123456+00:00"),
        ("2026-10-08T01:00:00.123456", "2026-10-08T01:00:00.123456+00:00"),
    ],
)
def test_export_rejects_invalid_or_different_created_instants_without_replacing_file(
    tmp_path: Path, metadata_time: str, header_time: str
) -> None:
    body = _snapshot(metadata_overrides={"created_at": metadata_time})
    client, _, streams = _export_client(
        body,
        0,
        headers={"X-Export-Created-At": header_time},
    )
    destination = tmp_path / "existing.ndjson"
    destination.write_bytes(b"old destination")

    with pytest.raises(OpendataClientError):
        client.export_provider_model(
            source="fred",
            model="FredSeries",
            query={},
            destination=destination,
            overwrite=True,
        )

    assert destination.read_bytes() == b"old destination"
    assert list(tmp_path.iterdir()) == [destination]
    assert streams and streams[0].close_count == 1


def test_export_preserves_bls_m13_footnotes_and_boolean_fields(tmp_path: Path) -> None:
    rows = _bls_rows()
    body = _snapshot(rows, source="bls", model="BlsSeries", domain="bls_series")
    client, _, _ = _export_client(body, len(rows))
    destination = tmp_path / "bls.ndjson"

    client.export_provider_model(
        source="bls",
        model="BlsSeries",
        query=_default_query(filters={"series_id": "LNS14000000"}),
        destination=destination,
    )

    assert destination.read_bytes() == body
    exported_row = json.loads(body.splitlines()[1])["data"]
    assert type(exported_row["year"]) is int and exported_row["year"] == 2025
    assert exported_row["period"] == "M13"
    assert exported_row["footnotes"] == [
        {"code": "P", "text": "Preliminary."},
        {"code": None, "text": None},
    ]
    assert exported_row["preliminary"] is True


def test_export_preserves_fmp_big_integer_negative_zero_and_nulls(tmp_path: Path) -> None:
    rows = _fmp_rows()
    body = _snapshot(rows, source="fmp", model="EquityHistorical", domain="equity_historical")
    client, _, _ = _export_client(body, len(rows))
    destination = tmp_path / "fmp.ndjson"

    client.export_provider_model(
        source="fmp",
        model="EquityHistorical",
        query=_default_query(filters={"symbol": "AAPL"}),
        destination=destination,
    )

    assert destination.read_bytes() == body
    exported_rows = [json.loads(line)["data"] for line in body.splitlines()[1:-1]]
    assert exported_rows[0]["volume"] == 2**256 + 17
    assert type(exported_rows[0]["volume"]) is int
    negative_zero = exported_rows[1]["volume"]
    assert negative_zero == 0.0 and math.copysign(1.0, negative_zero) == -1.0
    assert exported_rows[1]["currency"] is None
    assert exported_rows[1]["volume_unit"] is None


def test_empty_export_is_valid_and_does_not_close_injected_http_client(tmp_path: Path) -> None:
    body = _snapshot()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        stream = _ChunkStream([body[:11], body[11:]])
        return httpx.Response(200, headers=_headers(body, 0), stream=stream, request=request)

    external_client = httpx.Client(
        base_url="http://api.test",
        headers={"X-API-Key": "external-key"},
        transport=httpx.MockTransport(handler),
    )
    client = OpendataClient("http://api.test", api_key="sdk-key", http_client=external_client)
    destination = tmp_path / "empty.ndjson"

    result = client.export_provider_model(
        source="fred",
        model="FredSeries",
        query={"filters": {}},
        destination=destination,
    )
    client.close()

    assert result["row_count"] == 0
    assert destination.read_bytes() == body
    assert seen[0].headers["x-api-key"] == "external-key"
    assert not external_client.is_closed
    external_client.close()


@pytest.mark.parametrize(
    ("source", "model", "query", "overwrite"),
    [
        ("auto", "FredSeries", {}, False),
        ("fred", "FredSeries/../other", {}, False),
        ("fred", "FredSeries", {"unknown": 1}, False),
        ("fred", "FredSeries", {"max_records": True}, False),
        ("fred", "FredSeries", {"max_records": 200_001}, False),
        ("fred", "FredSeries", {"max_bytes": False}, False),
        ("fred", "FredSeries", {"max_bytes": 64 * 1024 * 1024 + 1}, False),
        ("fred", "FredSeries", {1: "numeric key"}, False),
        ("fred", "FredSeries", {"filters": {None: "bad key"}}, False),
        ("fred", "FredSeries", {"filters": {"x": float("nan")}}, False),
        ("fred", "FredSeries", {"filters": {"x": date(2024, 1, 1)}}, False),
        ("fred", "FredSeries", {}, 1),
    ],
)
def test_export_invalid_arguments_fail_before_http(
    tmp_path: Path,
    source: str,
    model: str,
    query: Any,
    overwrite: Any,
) -> None:
    client, requests, _ = _export_client(b"", 0)
    destination = tmp_path / "invalid.ndjson"

    with pytest.raises(ValueError):
        client.export_provider_model(
            source=source,
            model=model,
            query=query,
            destination=destination,
            overwrite=overwrite,
        )

    assert requests == []
    assert not destination.exists()


def test_export_rejects_cycles_and_excessive_query_depth_before_http(tmp_path: Path) -> None:
    client, requests, _ = _export_client(b"", 0)
    cycle: list[Any] = []
    cycle.append(cycle)
    deep: Any = {}
    for _ in range(258):
        deep = {"nested": deep}

    for query in ({"filters": {"cycle": cycle}}, {"filters": deep}):
        with pytest.raises(ValueError):
            client.export_provider_model(
                source="fred",
                model="FredSeries",
                query=query,
                destination=tmp_path / "invalid.ndjson",
            )

    assert requests == []


def test_export_does_not_create_missing_destination_parent(tmp_path: Path) -> None:
    client, requests, _ = _export_client(b"", 0)
    parent = tmp_path / "not-created"
    destination = parent / "export.ndjson"

    with pytest.raises(ValueError):
        client.export_provider_model(
            source="fred",
            model="FredSeries",
            query={},
            destination=destination,
        )

    assert requests == []
    assert not parent.exists()


@pytest.mark.parametrize("target_kind", ["file", "directory", "symlink"])
def test_export_existing_destination_is_rejected_before_http(
    tmp_path: Path, target_kind: str
) -> None:
    client, requests, _ = _export_client(b"", 0)
    destination = tmp_path / "target"
    if target_kind == "file":
        destination.write_bytes(b"keep")
    elif target_kind == "directory":
        destination.mkdir()
    else:
        existing = tmp_path / "original"
        existing.write_bytes(b"keep")
        destination.symlink_to(existing)

    with pytest.raises(ValueError):
        client.export_provider_model(
            source="fred",
            model="FredSeries",
            query={},
            destination=destination,
        )

    assert requests == []
    if target_kind == "file":
        assert destination.read_bytes() == b"keep"
    if target_kind == "symlink":
        assert destination.is_symlink() and existing.read_bytes() == b"keep"


@pytest.mark.parametrize("target_kind", ["directory", "symlink"])
def test_export_overwrite_also_rejects_directories_and_symlinks_before_http(
    tmp_path: Path, target_kind: str
) -> None:
    client, requests, _ = _export_client(b"", 0)
    destination = tmp_path / "target"
    if target_kind == "directory":
        destination.mkdir()
    else:
        existing = tmp_path / "original"
        existing.write_bytes(b"keep")
        destination.symlink_to(existing)

    with pytest.raises(ValueError):
        client.export_provider_model(
            source="fred",
            model="FredSeries",
            query={},
            destination=destination,
            overwrite=True,
        )

    assert requests == []
    if target_kind == "symlink":
        assert destination.is_symlink() and existing.read_bytes() == b"keep"


@pytest.mark.parametrize(
    ("status_code", "error_type"),
    [
        (400, InvalidQueryError),
        (401, AuthenticationError),
        (403, PermissionDeniedError),
        (404, NotFoundError),
        (429, OpendataClientError),
        (501, UnsupportedQueryError),
        (503, OpendataClientError),
        (504, OpendataClientError),
    ],
)
def test_export_http_errors_are_mapped_without_reflecting_secrets(
    tmp_path: Path, status_code: int, error_type: type[OpendataClientError]
) -> None:
    client, requests, _ = _export_client(
        b"",
        0,
        status_code=status_code,
        error_body=b"query-secret api-key-secret export-secret",
    )
    destination = tmp_path / "error.ndjson"

    with pytest.raises(error_type) as caught:
        client.export_provider_model(
            source="fred",
            model="FredSeries",
            query={},
            destination=destination,
        )

    message = str(caught.value)
    assert str(status_code) in message
    assert "query-secret" not in message
    assert "api-key-secret" not in message
    assert "export-secret" not in message
    assert caught.value.__cause__ is None
    assert len(requests) == 1
    assert not destination.exists()


@pytest.mark.parametrize(
    ("header_name", "header_value"),
    [
        ("Content-Type", "application/json"),
        ("Content-Encoding", "gzip"),
        ("X-Content-SHA256", "f" * 64),
        ("X-Row-Count", "2"),
        ("X-NDJSON-Schema-Version", "2"),
        ("X-Snapshot-Complete", "false"),
        ("X-Snapshot-Consistency", "read_committed"),
        ("X-Completeness", "COMPLETE"),
        ("X-Export-Created-At", "not-a-timestamp"),
        ("Content-Length", "01"),
        ("Content-Length", "999999999"),
    ],
)
def test_export_rejects_bad_headers_and_cleans_temporary_file(
    tmp_path: Path, header_name: str, header_value: str
) -> None:
    body = _snapshot()
    client, _, streams = _export_client(
        body,
        0,
        headers={header_name: header_value},
    )
    destination = tmp_path / "bad-header.ndjson"

    with pytest.raises(OpendataClientError):
        client.export_provider_model(
            source="fred",
            model="FredSeries",
            query={},
            destination=destination,
        )

    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []
    assert streams and streams[0].close_count == 1


@pytest.mark.parametrize(
    "body",
    [
        _snapshot(metadata_overrides={"source": "bls"}),
        _snapshot(metadata_overrides={"unexpected": "field"}),
        _snapshot(metadata_overrides={"created_at": "2026-10-08T00:00:01Z"}),
        _snapshot(metadata_overrides={"verified": 1}),
        _snapshot(metadata_overrides={"domain": ""}),
        _snapshot(summary_rows=1),
        _snapshot()[:-1],
        _snapshot_from_lines(
            [
                _json_line(_metadata()).replace(
                    b'"kind":"metadata"', b'"kind":"metadata","kind":"metadata"', 1
                ),
                _json_line({"kind": "summary", "rows": 0, "snapshot_complete": True}),
            ]
        ),
        _snapshot_from_lines(
            [
                _json_line(_metadata()),
                b'{"kind":"row","data":{"value":NaN}}',
                _json_line({"kind": "summary", "rows": 1, "snapshot_complete": True}),
            ]
        ),
        _snapshot_from_lines(
            [
                _json_line(_metadata()),
                b'{"kind":"row","data":{"value":Infinity}}',
                _json_line({"kind": "summary", "rows": 1, "snapshot_complete": True}),
            ]
        ),
        _snapshot_from_lines(
            [
                _json_line(_metadata()),
                b'{"kind":"row","data":{"value":1e999}}',
                _json_line({"kind": "summary", "rows": 1, "snapshot_complete": True}),
            ]
        ),
        _snapshot_from_lines(
            [
                _json_line(_metadata()),
                b'{"kind":"row","data":{"field":1,"field":2}}',
                _json_line({"kind": "summary", "rows": 1, "snapshot_complete": True}),
            ]
        ),
        _deep_snapshot(),
        _snapshot_from_lines(
            [
                _json_line(_metadata()),
                _json_line({"kind": "row", "data": {}, "extra": True}),
                _json_line({"kind": "summary", "rows": 1, "snapshot_complete": True}),
            ]
        ),
        _snapshot_from_lines(
            [
                _json_line(_metadata()),
                _json_line({"kind": "row", "data": []}),
                _json_line({"kind": "summary", "rows": 1, "snapshot_complete": True}),
            ]
        ),
        _snapshot_from_lines(
            [
                _json_line(_metadata()),
                _json_line(_metadata()),
                _json_line({"kind": "summary", "rows": 0, "snapshot_complete": True}),
            ]
        ),
        _snapshot_from_lines(
            [
                _json_line(_metadata()),
                _json_line({"kind": "summary", "rows": 0, "snapshot_complete": True}),
                _json_line({"kind": "row", "data": {}}),
            ]
        ),
        _snapshot_from_lines(
            [
                _json_line(_metadata()),
                b'{"kind":"row","data":{"x":"\xff"}}',
                _json_line({"kind": "summary", "rows": 1, "snapshot_complete": True}),
            ]
        ),
    ],
    ids=[
        "identity-mismatch",
        "metadata-extra-field",
        "metadata-created-at-mismatch",
        "metadata-nonboolean-verified",
        "metadata-empty-domain",
        "summary-count-mismatch",
        "missing-final-newline",
        "duplicate-json-key",
        "nan",
        "infinity",
        "overflow-to-infinity",
        "nested-duplicate-json-key",
        "excessive-depth",
        "extra-row-field",
        "non-object-row-data",
        "second-metadata",
        "after-summary",
        "invalid-utf8",
    ],
)
def test_export_rejects_invalid_ndjson_without_publishing(tmp_path: Path, body: bytes) -> None:
    client, _, streams = _export_client(body, 1 if b'"kind":"row"' in body else 0)
    destination = tmp_path / "invalid-body.ndjson"

    with pytest.raises(OpendataClientError) as caught:
        client.export_provider_model(
            source="fred",
            model="FredSeries",
            query={},
            destination=destination,
        )

    assert "secret" not in str(caught.value).casefold()
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []
    assert streams and streams[0].close_count == 1


def test_export_enforces_requested_limits_before_reading_body(tmp_path: Path) -> None:
    body = _snapshot(_fred_rows())
    client, _, streams = _export_client(body, 2)
    destination = tmp_path / "over-limit.ndjson"

    with pytest.raises(OpendataClientError):
        client.export_provider_model(
            source="fred",
            model="FredSeries",
            query={"max_records": 1, "max_bytes": len(body) - 1},
            destination=destination,
        )

    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []
    assert streams and streams[0].yielded == 0 and streams[0].close_count == 1


def test_export_enforces_total_size_while_streaming_even_with_false_length(
    tmp_path: Path,
) -> None:
    body = _snapshot()
    client, _, streams = _export_client(
        body,
        0,
        headers={"Content-Length": "100"},
    )
    destination = tmp_path / "lying-length.ndjson"

    with pytest.raises(OpendataClientError):
        client.export_provider_model(
            source="fred",
            model="FredSeries",
            query={"max_bytes": 100},
            destination=destination,
        )

    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []
    assert streams and streams[0].yielded > 0 and streams[0].close_count == 1


def test_export_stream_failure_is_redacted_and_removes_partial_temp_file(tmp_path: Path) -> None:
    body = _snapshot(_fred_rows())
    client, _, streams = _export_client(
        body,
        2,
        chunks=[body[:32]],
        stream_error=RuntimeError("stream-secret query-secret key-secret"),
    )
    destination = tmp_path / "interrupted.ndjson"

    with pytest.raises(OpendataClientError) as caught:
        client.export_provider_model(
            source="fred",
            model="FredSeries",
            query={},
            destination=destination,
        )

    message = str(caught.value)
    assert "stream-secret" not in message
    assert "query-secret" not in message
    assert "key-secret" not in message
    assert caught.value.__cause__ is None
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []
    assert streams and streams[0].close_count == 1


def test_export_local_io_failure_is_redacted_and_removes_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = _snapshot()
    client, _, streams = _export_client(body, 0)
    destination = tmp_path / "io-failure.ndjson"

    def fail_fsync(_fd: int) -> None:
        raise OSError("path-secret api-key-secret")

    monkeypatch.setattr(os, "fsync", fail_fsync)
    with pytest.raises(OpendataClientError) as caught:
        client.export_provider_model(
            source="fred",
            model="FredSeries",
            query={},
            destination=destination,
        )

    assert "path-secret" not in str(caught.value)
    assert "api-key-secret" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []
    assert streams and streams[0].close_count == 1


def test_export_no_overwrite_publish_does_not_clobber_racing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = _snapshot()
    client, _, _ = _export_client(body, 0)
    destination = tmp_path / "race.ndjson"
    real_link = os.link

    def create_competitor(source: Any, target: Any, **kwargs: Any) -> None:
        Path(target).write_bytes(b"competitor")
        real_link(source, target, **kwargs)

    monkeypatch.setattr(os, "link", create_competitor)
    with pytest.raises(OpendataClientError):
        client.export_provider_model(
            source="fred",
            model="FredSeries",
            query={},
            destination=destination,
        )

    assert destination.read_bytes() == b"competitor"
    assert list(tmp_path.iterdir()) == [destination]


def test_export_overwrite_replaces_only_a_regular_file(tmp_path: Path) -> None:
    body = _snapshot()
    client, _, _ = _export_client(body, 0)
    destination = tmp_path / "replace.ndjson"
    destination.write_bytes(b"old")

    client.export_provider_model(
        source="fred",
        model="FredSeries",
        query={},
        destination=destination,
        overwrite=True,
    )

    assert destination.read_bytes() == body
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
