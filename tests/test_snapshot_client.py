"""Behavior tests for the fixed-publication v2 snapshot SDK contract."""

from __future__ import annotations

import copy
import json
import time
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from opendata_client.snapshots import (
    Snapshot,
    SnapshotAuthenticationError,
    SnapshotClient,
    SnapshotClientError,
    SnapshotDeadlineError,
    SnapshotExpiredError,
    SnapshotHTTPError,
    SnapshotInvalidRequestError,
    SnapshotLimitError,
    SnapshotNotFoundError,
    SnapshotPermissionError,
    SnapshotProtocolError,
    SnapshotRateLimitError,
    SnapshotScopeError,
    SnapshotTooLargeError,
    SnapshotUnavailableError,
)

if TYPE_CHECKING:
    from collections.abc import Callable


IDENTITY_SEMANTICS: dict[str, Any] = {
    "version": 1,
    "business_key_fields": ["symbol"],
    "identity_key_encoding": "canonical-json-array-v1",
    "revision": "latest non-deleted revision per business_key at the pinned publication sequence",
    "ordering_fields": ["business_date", "symbol"],
    "storage_order": [
        {
            "field": "business_date",
            "direction": "asc",
            "nulls": "last",
            "comparison": "native",
        },
        {"field": "business_key", "direction": "asc", "comparison": "binary_lexicographic"},
    ],
    "temporal_semantics": "business_date",
    "time_field": "business_date",
    "temporal_fields": {
        "business_date": (
            "business-date bucket; null ordering last (route rejects null time values)"
        ),
        "observed_at": "source observation time",
        "source_published_at": "source publication time",
        "available_at": "platform availability time",
        "event_at": "source event time",
    },
}


def _snapshot(**changes: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "snapshot_id": "b" * 32,
        "dataset_id": "stock_daily",
        "publication_id": "c" * 32,
        "sequence": 17,
        "schema_version": "2.0.0",
        "semantic_version": "2.0.0",
        "source": {"source_id": "test-source", "source": "fixture"},
        "schema": [
            {"name": "symbol", "data_type": "string", "nullable": False},
            {"name": "business_date", "data_type": "date", "nullable": False},
            {"name": "close", "data_type": "number", "nullable": True},
            {"name": "volume", "data_type": "integer", "nullable": True},
        ],
        "quality": {"status": "passed", "report_id": "quality-001", "issue_count": 0},
        "field_units": {"close": "CNY/share", "volume": "share"},
        "field_currencies": {"close": "CNY"},
        "field_adjustments": {"close": "raw"},
        "currency": "CNY",
        "adjustment_basis": "raw",
        "asset_class": "equity",
        "frequency": "1d",
        "record_kind": "time_series",
        "business_key_fields": ["symbol"],
        "ordering_fields": ["business_date", "symbol"],
        "storage_order": [
            {
                "field": "business_date",
                "direction": "asc",
                "nulls": "last",
                "comparison": "native",
            },
            {"field": "business_key", "direction": "asc", "comparison": "binary_lexicographic"},
        ],
        "identity_key_encoding": "canonical-json-array-v1",
        "filter_fields": [],
        "time_field": "business_date",
        "temporal_semantics": "business_date",
        "timezone": "Asia/Shanghai",
        "pit_policy": "unsupported",
        "source_available_at_field": None,
        "calendar_id": None,
        "contract_sha256": "d" * 64,
        "window": {"start": "2024-01-01", "end": "2024-02-01", "end_exclusive": True},
        "page_size": 2,
        "as_of": "2024-02-02T00:00:00Z",
        "created_at": "2024-02-02T00:00:01Z",
        "expires_at": "2024-02-03T00:00:01Z",
        "record_count": 3,
        "selection_sha256": "a" * 64,
        "content_sha256": "b" * 64,
        "identity_semantics": copy.deepcopy(IDENTITY_SEMANTICS),
    }
    data.update(changes)
    return data


def _row(
    key: str,
    business_date: str,
    close: float = 1.0,
    *,
    publication_id: str = "c" * 32,
    publication_sequence: int = 17,
) -> dict[str, Any]:
    return {
        "dataset_id": "stock_daily",
        "business_key": json.dumps([key], ensure_ascii=False, separators=(",", ":")),
        "revision": 1,
        "publication_id": publication_id,
        "publication_sequence": publication_sequence,
        "business_date": business_date,
        "event_at": None,
        "observed_at": "2024-01-02T15:00:00+08:00",
        "source_published_at": None,
        "available_at": "2024-02-02T00:00:00Z",
        "payload": {"symbol": key, "business_date": business_date, "close": close},
        "record_hash": "c" * 64,
    }


def _records_page(
    rows: list[dict[str, Any]],
    *,
    snapshot: dict[str, Any] | None = None,
    next_cursor: str | None = None,
    has_more: bool = False,
    **changes: Any,
) -> dict[str, Any]:
    pinned = _snapshot() if snapshot is None else snapshot
    result: dict[str, Any] = copy.deepcopy(pinned)
    result.update(
        {
            "rows": rows,
            "count": len(rows),
            "next_cursor": next_cursor,
            "has_more": has_more,
            "meta": {
                "snapshot": copy.deepcopy(pinned),
                "ordering_fields": copy.deepcopy(pinned["ordering_fields"]),
                "storage_order": copy.deepcopy(pinned["storage_order"]),
                "identity_key_encoding": pinned["identity_key_encoding"],
            },
        }
    )
    result.update(changes)
    return result


def _client(handler: Callable[[httpx.Request], httpx.Response], **kwargs: Any) -> SnapshotClient:
    return SnapshotClient(
        "https://data.example",
        api_key="od-test-key",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


class _TrackingStream(httpx.SyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.read_count = 0
        self.closed = False

    def __iter__(self):
        for chunk in self.chunks:
            self.read_count += 1
            yield chunk

    def close(self) -> None:
        self.closed = True


class TestSnapshotCreation:
    @pytest.mark.parametrize(
        "candidate_url",
        [
            "file:///tmp/data",
            "https://user:secret@data.example",
            "https://data.example/?token=secret",
            "https://data.example/#private",
        ],
    )
    def test_client_rejects_unsafe_base_url_parts(self, candidate_url: str) -> None:
        with pytest.raises(ValueError, match="base_url"):
            SnapshotClient(candidate_url, api_key="od-test-key")

    @pytest.mark.parametrize(
        "configuration",
        [
            {"timeout": True},
            {"timeout": float("nan")},
            {"timeout": 601},
            {"deadline": 3_601},
            {"max_pages": 0},
            {"max_rows": 1.5},
            {"max_bytes": -1},
            {"api_key": "bad\nkey"},
        ],
    )
    def test_client_rejects_nonfinite_or_unbounded_configuration(
        self, configuration: dict[str, Any]
    ) -> None:
        kwargs: dict[str, Any] = {"api_key": "od-test-key", **configuration}
        with pytest.raises(ValueError):
            SnapshotClient("https://data.example", **kwargs)

    def test_client_rejects_two_transport_owners(self) -> None:
        http_client = httpx.Client()
        try:
            with pytest.raises(ValueError, match="either transport or http_client"):
                SnapshotClient(
                    "https://data.example",
                    api_key="od-test-key",
                    transport=httpx.MockTransport(lambda _: httpx.Response(200)),
                    http_client=http_client,
                )
        finally:
            http_client.close()

    @pytest.mark.parametrize(
        ("dataset_id", "start_date", "end_date", "page_size"),
        [
            ("../private", "2024-01-01", "2024-02-01", 1),
            ("stock_daily", "2024-1-01", "2024-02-01", 1),
            ("stock_daily", "2024-01-01", "2024-02-30", 1),
            ("stock_daily", "2024-02-01", "2024-01-01", 1),
            ("stock_daily", "2024-01-01", "2024-02-01", 0),
            ("stock_daily", "2024-01-01", "2024-02-01", True),
        ],
    )
    def test_create_rejects_invalid_scope_before_network_io(
        self, dataset_id: str, start_date: str, end_date: str, page_size: int
    ) -> None:
        calls = 0

        def handler(_: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(201, json=_snapshot())

        with _client(handler) as client, pytest.raises(ValueError):
            client.create_snapshot(
                dataset_id,
                start_date=start_date,
                end_date=end_date,
                page_size=page_size,
                expected_identity_semantics=IDENTITY_SEMANTICS,
            )
        assert calls == 0

    def test_post_pins_exact_scope_without_retry_semantics(self) -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["method"] = request.method
            seen["url"] = str(request.url)
            seen["headers"] = dict(request.headers)
            seen["body"] = request.read()
            return httpx.Response(201, json=_snapshot())

        with _client(handler) as client:
            snapshot = client.create_snapshot(
                "stock_daily",
                start_date="2024-01-01",
                end_date="2024-02-01",
                page_size=2,
                publication_id="c" * 32,
                expected_identity_semantics=IDENTITY_SEMANTICS,
            )

        assert seen["method"] == "POST"
        assert httpx.URL(seen["url"]).path == "/api/v2/snapshots"
        assert seen["headers"]["x-api-key"] == "od-test-key"
        assert "idempotency-key" not in seen["headers"]
        assert seen["body"] == (
            b'{"dataset_id":"stock_daily","start_date":"2024-01-01",'
            b'"end_date":"2024-02-01","page_size":2,"publication_id":"' + b"c" * 32 + b'"}'
        )
        assert snapshot.snapshot_id == "b" * 32
        assert snapshot.publication_id == "c" * 32
        assert snapshot.window.start.isoformat() == "2024-01-01"
        assert snapshot.window.end.isoformat() == "2024-02-01"

    @pytest.mark.parametrize(
        "changes",
        [
            {"dataset_id": "other_dataset"},
            {"publication_id": "d" * 32},
            {"window": {"start": "2024-01-01", "end": "2024-02-01", "end_exclusive": False}},
            {"identity_semantics": {"key_field": "symbol"}},
        ],
    )
    def test_create_rejects_changed_scope_or_semantics(self, changes: dict[str, Any]) -> None:
        requested_publication = "c" * 32 if "publication_id" in changes else None
        with (
            _client(lambda _: httpx.Response(201, json=_snapshot(**changes))) as client,
            pytest.raises((SnapshotScopeError, SnapshotProtocolError)),
        ):
            client.create_snapshot(
                "stock_daily",
                start_date="2024-01-01",
                end_date="2024-02-01",
                publication_id=requested_publication,
                expected_identity_semantics=IDENTITY_SEMANTICS,
            )

    def test_create_does_not_retry_a_post_without_idempotency_support(self) -> None:
        calls = 0

        def handler(_: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(503, json={"error": {"message": "private detail"}})

        with _client(handler) as client, pytest.raises(SnapshotUnavailableError) as caught:
            client.create_snapshot(
                "stock_daily",
                start_date="2024-01-01",
                end_date="2024-02-01",
                expected_identity_semantics=IDENTITY_SEMANTICS,
            )
        assert calls == 1
        assert "private detail" not in str(caught.value)

    def test_scope_and_semantics_are_required_before_network_io(self) -> None:
        calls = 0

        def handler(_: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json=_snapshot())

        with (
            _client(handler) as client,
            pytest.raises(ValueError, match="expected_identity_semantics"),
        ):
            client.create_snapshot(
                "stock_daily",
                start_date="2024-01-01",
                end_date="2024-02-01",
                expected_identity_semantics={},
            )
        assert calls == 0

    @pytest.mark.parametrize(
        "semantic_change",
        [
            {"version": 2},
            {"identity_key_encoding": "provider-defined"},
            {"business_key_fields": ["symbol", "symbol"]},
            {"ordering_fields": []},
            {"temporal_fields": {}},
            {"storage_order": []},
        ],
    )
    def test_unsupported_expected_semantics_fail_before_network_io(
        self, semantic_change: dict[str, Any]
    ) -> None:
        expected = copy.deepcopy(IDENTITY_SEMANTICS)
        expected.update(semantic_change)
        calls = 0

        def handler(_: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(201, json=_snapshot())

        with _client(handler) as client, pytest.raises(ValueError, match="identity"):
            client.create_snapshot(
                "stock_daily",
                start_date="2024-01-01",
                end_date="2024-02-01",
                expected_identity_semantics=expected,
            )
        assert calls == 0

    def test_incomplete_or_malformed_snapshot_metadata_fails_closed(self) -> None:
        invalid_payloads = [
            {},
            _snapshot(snapshot_id=""),
            _snapshot(dataset_id="../private"),
            _snapshot(publication_id="bad\nidentity"),
            _snapshot(sequence=True),
            _snapshot(schema_version=""),
            _snapshot(semantic_version=""),
            _snapshot(source={"source_id": "test-source"}),
            _snapshot(schema="not-a-list"),
            _snapshot(schema=[{"name": "symbol", "data_type": "unknown", "nullable": False}]),
            _snapshot(quality={"status": "passed", "report_id": None, "issue_count": True}),
            _snapshot(adjustment_basis=""),
            _snapshot(identity_key_encoding="not-canonical"),
            _snapshot(field_units={"unregistered": "unit"}),
            _snapshot(time_field="unregistered"),
            _snapshot(window={"start": "2024-02-01", "end": "2024-01-01", "end_exclusive": True}),
            _snapshot(window={"start": "2024-01-01", "end": "2024-02-01", "end_exclusive": False}),
            _snapshot(page_size=0),
            _snapshot(record_count=True),
            _snapshot(as_of="2024-02-02T00:00:00"),
            _snapshot(identity_semantics=None),
            _snapshot(contract_sha256="A" * 64),
        ]
        for payload in invalid_payloads:

            def handler(
                _: httpx.Request, response_payload: dict[str, Any] = payload
            ) -> httpx.Response:
                return httpx.Response(201, json=response_payload)

            with _client(handler) as client, pytest.raises(SnapshotProtocolError):
                client.create_snapshot(
                    "stock_daily",
                    start_date="2024-01-01",
                    end_date="2024-02-01",
                    expected_identity_semantics=IDENTITY_SEMANTICS,
                )


class TestSnapshotReads:
    def test_iter_reads_every_page_with_one_snapshot_and_no_scope_drift(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            cursor = request.url.params.get("cursor")
            if cursor is None:
                data = _records_page(
                    [
                        _row("equity|600519|2024-01-02", "2024-01-02"),
                        _row("equity|600519|2024-01-03", "2024-01-03"),
                    ],
                    next_cursor="opaque-cursor-1",
                    has_more=True,
                )
            else:
                data = _records_page([_row("equity|600519|2024-01-04", "2024-01-04")])
            return httpx.Response(200, json=data)

        with _client(handler) as client:
            rows = list(
                client.iter_records(
                    _descriptor(),
                    expected_identity_semantics=IDENTITY_SEMANTICS,
                )
            )

        assert [row["business_date"] for row in rows] == [
            "2024-01-02",
            "2024-01-03",
            "2024-01-04",
        ]
        assert len(requests) == 2
        assert all(
            request.url.path == "/api/v2/datasets/stock_daily/records" for request in requests
        )
        assert dict(requests[0].url.params) == {"snapshot_id": "b" * 32}
        assert dict(requests[1].url.params) == {
            "snapshot_id": "b" * 32,
            "cursor": "opaque-cursor-1",
        }

    def test_list_records_by_id_reauthorizes_dataset_scope(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.path == f"/api/v2/snapshots/{'b' * 32}":
                return httpx.Response(200, json=_snapshot(record_count=0))
            return httpx.Response(
                200,
                json=_records_page([], snapshot=_snapshot(record_count=0)),
            )

        with _client(handler) as client:
            page = client.list_records(
                "b" * 32,
                dataset_id="stock_daily",
                expected_identity_semantics=IDENTITY_SEMANTICS,
            )
        assert page.rows == ()
        assert dict(seen[0].url.params) == {"dataset_id": "stock_daily"}

    def test_terminal_empty_snapshot_is_a_valid_complete_read(self) -> None:
        snapshot = _snapshot(record_count=0)

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=_records_page([], snapshot=snapshot),
            )

        with _client(handler) as client:
            rows = list(
                client.iter_records(
                    _descriptor(record_count=0),
                    expected_identity_semantics=IDENTITY_SEMANTICS,
                )
            )
        assert rows == []

    def test_cross_page_publication_change_fails_closed(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(201, json=_snapshot())
            if request.url.params.get("cursor") is None:
                return httpx.Response(
                    200,
                    json=_records_page(
                        [_row("a", "2024-01-02"), _row("b", "2024-01-03")],
                        next_cursor="cursor-1",
                        has_more=True,
                    ),
                )
            changed = _snapshot(publication_id="d" * 32)
            return httpx.Response(
                200,
                json=_records_page(
                    [_row("c", "2024-01-04")],
                    snapshot=changed,
                ),
            )

        with _client(handler) as client:
            snapshot = client.create_snapshot(
                "stock_daily",
                start_date="2024-01-01",
                end_date="2024-02-01",
                page_size=2,
                expected_identity_semantics=IDENTITY_SEMANTICS,
            )
            iterator = client.iter_records(
                snapshot,
                expected_identity_semantics=IDENTITY_SEMANTICS,
            )
            assert next(iterator)["payload"]["symbol"] == "a"
            assert next(iterator)["payload"]["symbol"] == "b"
            with pytest.raises(SnapshotScopeError):
                next(iterator)

    def test_bad_page_count_is_rejected(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_records_page([_row("a", "2024-01-02")], count=0))

        with (
            _client(handler) as client,
            pytest.raises(SnapshotProtocolError, match="count or cursor"),
        ):
            client.list_records(
                _descriptor(),
                expected_identity_semantics=IDENTITY_SEMANTICS,
            )

    def test_page_metadata_contract_must_match_snapshot_contract(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            page = _records_page([], snapshot=_snapshot(record_count=0))
            page["meta"]["ordering_fields"] = ["business_key"]
            return httpx.Response(200, json=page)

        with (
            _client(handler) as client,
            pytest.raises(SnapshotProtocolError, match="metadata envelope"),
        ):
            client.list_records(
                _descriptor(record_count=0),
                expected_identity_semantics=IDENTITY_SEMANTICS,
            )

    def test_row_identity_and_temporal_corruption_is_rejected(self) -> None:
        invalid_rows: list[tuple[Any, type[SnapshotProtocolError]]] = []
        row = _row("a", "2024-01-02")
        invalid_rows.append(([], SnapshotProtocolError))
        missing_field = copy.deepcopy(row)
        missing_field.pop("record_hash")
        invalid_rows.append((missing_field, SnapshotProtocolError))
        missing_origin_sequence = copy.deepcopy(row)
        missing_origin_sequence.pop("publication_sequence")
        invalid_rows.append((missing_origin_sequence, SnapshotProtocolError))
        invalid_rows.append(({**row, "business_key": ""}, SnapshotProtocolError))
        invalid_rows.append(({**row, "dataset_id": "other"}, SnapshotScopeError))
        invalid_rows.append(({**row, "publication_id": "d" * 32}, SnapshotScopeError))
        invalid_rows.append(({**row, "revision": True}, SnapshotProtocolError))
        invalid_rows.append(({**row, "publication_sequence": True}, SnapshotProtocolError))
        invalid_rows.append(({**row, "publication_sequence": 0}, SnapshotProtocolError))
        invalid_rows.append(({**row, "business_date": "2024-02-01"}, SnapshotScopeError))
        invalid_rows.append(({**row, "observed_at": "not-a-timestamp"}, SnapshotProtocolError))
        invalid_rows.append(({**row, "payload": "not-an-object"}, SnapshotProtocolError))
        invalid_rows.append(({**row, "business_key": "not-json"}, SnapshotProtocolError))
        invalid_rows.append(({**row, "business_key": '["a", "b"]'}, SnapshotProtocolError))
        invalid_rows.append(({**row, "business_key": '[ "a" ]'}, SnapshotProtocolError))
        invalid_rows.append(({**row, "business_key": '["b"]'}, SnapshotProtocolError))
        invalid_rows.append(({**row, "record_hash": "bad"}, SnapshotProtocolError))
        invalid_rows.append(
            (
                {**row, "publication_id": "not-a-hex-origin", "publication_sequence": 16},
                SnapshotProtocolError,
            )
        )
        invalid_rows.append(({**row, "publication_sequence": 18}, SnapshotScopeError))
        invalid_rows.append(
            (
                {**row, "publication_id": "d" * 32, "publication_sequence": 17},
                SnapshotScopeError,
            )
        )
        invalid_rows.append(({"not": "a row"}, SnapshotProtocolError))

        for invalid_row, error_type in invalid_rows:

            def handler(_: httpx.Request, row_value: Any = invalid_row) -> httpx.Response:
                return httpx.Response(
                    200,
                    json=_records_page(
                        [row_value],
                        snapshot=_snapshot(record_count=1),
                    ),
                )

            with _client(handler) as client, pytest.raises(error_type):
                client.list_records(
                    _descriptor(record_count=1),
                    expected_identity_semantics=IDENTITY_SEMANTICS,
                )

    def test_row_from_earlier_publication_is_valid_inside_pinned_snapshot(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            row = _row(
                "a",
                "2024-01-02",
                publication_id="a" * 32,
                publication_sequence=16,
            )
            return httpx.Response(
                200,
                json=_records_page([row], snapshot=_snapshot(record_count=1)),
            )

        with _client(handler) as client:
            rows = client.list_records(
                _descriptor(record_count=1),
                expected_identity_semantics=IDENTITY_SEMANTICS,
            )

        assert rows.rows[0]["publication_id"] == "a" * 32
        assert rows.rows[0]["publication_sequence"] == 16

    def test_page_duplicate_keys_and_unstable_order_are_rejected(self) -> None:
        duplicate_rows = [_row("a", "2024-01-02"), _row("a", "2024-01-02")]
        reverse_order_rows = [_row("b", "2024-01-02"), _row("a", "2024-01-02")]
        for rows, message in (
            (duplicate_rows, "repeats a business_key"),
            (reverse_order_rows, "stable ordering"),
        ):

            def handler(
                _: httpx.Request, response_rows: list[dict[str, Any]] = rows
            ) -> httpx.Response:
                return httpx.Response(
                    200,
                    json=_records_page(
                        response_rows,
                        snapshot=_snapshot(record_count=2),
                    ),
                )

            with _client(handler) as client, pytest.raises(SnapshotProtocolError, match=message):
                client.list_records(
                    _descriptor(record_count=2),
                    expected_identity_semantics=IDENTITY_SEMANTICS,
                )

    def test_repeated_cursor_and_duplicate_business_key_are_rejected(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.params.get("cursor") is None:
                rows = [_row("a", "2024-01-02"), _row("b", "2024-01-03")]
            else:
                rows = [_row("c", "2024-01-04")]
            return httpx.Response(
                200,
                json=_records_page(rows, next_cursor="cursor-1", has_more=True),
            )

        with _client(handler) as client, pytest.raises(SnapshotProtocolError, match="cursor"):
            list(
                client.iter_records(
                    _descriptor(),
                    expected_identity_semantics=IDENTITY_SEMANTICS,
                )
            )

    @pytest.mark.parametrize("limit_name", ["max_pages", "max_rows", "max_bytes"])
    def test_limits_raise_instead_of_returning_a_partial_iterator(self, limit_name: str) -> None:
        limits = {limit_name: 1}

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=_records_page(
                    [_row("a", "2024-01-02"), _row("b", "2024-01-03")],
                    next_cursor="cursor-1",
                    has_more=True,
                ),
            )

        with _client(handler, **limits) as client, pytest.raises(SnapshotLimitError):
            list(
                client.iter_records(
                    _descriptor(),
                    expected_identity_semantics=IDENTITY_SEMANTICS,
                )
            )

    def test_total_deadline_is_checked_after_a_slow_mocked_network_response(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            time.sleep(0.03)
            return httpx.Response(
                200,
                json=_records_page([], snapshot=_snapshot(record_count=0)),
            )

        with _client(handler, deadline=0.01) as client, pytest.raises(SnapshotDeadlineError):
            client.list_records(
                _descriptor(record_count=0),
                expected_identity_semantics=IDENTITY_SEMANTICS,
            )


def _descriptor(**snapshot_changes: Any) -> Snapshot:
    # Build descriptors through the public response parser, matching the exact
    # immutable metadata shape that the HTTP create/get endpoints return.
    response = httpx.Response(200, json=_snapshot(**snapshot_changes))

    def handler(_: httpx.Request) -> httpx.Response:
        return response

    with _client(handler) as client:
        return client.get_snapshot(
            "b" * 32,
            dataset_id="stock_daily",
            expected_identity_semantics=IDENTITY_SEMANTICS,
        )


class TestSnapshotHTTPGuards:
    @pytest.mark.parametrize(
        ("status_code", "error_type"),
        [
            (403, SnapshotPermissionError),
            (404, SnapshotNotFoundError),
            (410, SnapshotExpiredError),
            (422, SnapshotInvalidRequestError),
            (413, SnapshotTooLargeError),
            (429, SnapshotRateLimitError),
            (500, SnapshotHTTPError),
        ],
    )
    def test_http_statuses_translate_to_stable_error_types(
        self, status_code: int, error_type: type[Exception]
    ) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(status_code, json={"error": {"message": "private detail"}})

        with _client(handler) as client, pytest.raises(error_type) as caught:
            client.list_records(
                _descriptor(),
                expected_identity_semantics=IDENTITY_SEMANTICS,
            )
        assert "private detail" not in str(caught.value)

    def test_get_retries_retryable_status_once_without_reading_error_body(self) -> None:
        attempts = 0

        def handler(_: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return httpx.Response(
                    503,
                    json={"error": {"message": "private detail"}},
                    headers={"retry-after": "0"},
                )
            return httpx.Response(
                200,
                json=_records_page([], snapshot=_snapshot(record_count=0)),
            )

        with _client(handler) as client:
            page = client.list_records(
                _descriptor(record_count=0),
                expected_identity_semantics=IDENTITY_SEMANTICS,
            )
        assert page.count == 0
        assert attempts == 2

    def test_get_parses_http_date_retry_after(self, monkeypatch: Any) -> None:
        from datetime import datetime, timedelta, timezone
        from email.utils import format_datetime

        import opendata_client.snapshots as snapshot_module

        monkeypatch.setattr(snapshot_module.time, "sleep", lambda _: None)
        attempts = 0
        retry_at = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=0.5), usegmt=True)

        def handler(_: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return httpx.Response(503, headers={"retry-after": retry_at})
            return httpx.Response(200, json=_records_page([], snapshot=_snapshot(record_count=0)))

        with _client(handler) as client:
            page = client.list_records(
                _descriptor(record_count=0),
                expected_identity_semantics=IDENTITY_SEMANTICS,
            )
        assert page.count == 0
        assert attempts == 2

    def test_unbounded_retry_after_and_retry_beyond_deadline_do_not_retry(self) -> None:
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(
                503, headers={"retry-after": request.url.params.get("wait", "bad")}
            )

        with _client(handler) as client, pytest.raises(SnapshotUnavailableError):
            client.list_records(
                _descriptor(record_count=0),
                expected_identity_semantics=IDENTITY_SEMANTICS,
            )
        assert attempts == 1

        attempts = 0

        def slow_retry(_: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(503, headers={"retry-after": "0.1"})

        with (
            _client(slow_retry, deadline=0.01) as client,
            pytest.raises(SnapshotDeadlineError, match="retry"),
        ):
            client.list_records(
                _descriptor(record_count=0),
                expected_identity_semantics=IDENTITY_SEMANTICS,
            )
        assert attempts == 1

    def test_invalid_content_type_and_network_failure_are_sanitized(self) -> None:
        with (
            _client(lambda _: httpx.Response(200, text="not-json")) as client,
            pytest.raises(SnapshotProtocolError, match="non-JSON"),
        ):
            client.list_records(
                _descriptor(record_count=0),
                expected_identity_semantics=IDENTITY_SEMANTICS,
            )

        def fail_connection(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("private host and token", request=request)

        with (
            _client(fail_connection) as client,
            pytest.raises(SnapshotClientError, match="request failed") as caught,
        ):
            client.list_records(
                _descriptor(record_count=0),
                expected_identity_semantics=IDENTITY_SEMANTICS,
            )
        assert "private host" not in str(caught.value)

    def test_http_error_does_not_read_or_expose_the_error_body(self) -> None:
        body = _TrackingStream([b"secret server detail"])

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(401, stream=body, headers={"content-type": "application/json"})

        with _client(handler) as client, pytest.raises(SnapshotAuthenticationError) as caught:
            client.list_records(
                _descriptor(),
                expected_identity_semantics=IDENTITY_SEMANTICS,
            )
        assert body.read_count == 0
        assert body.closed is True
        assert "secret" not in str(caught.value)

    def test_oversized_response_fails_before_consuming_remaining_chunks(self) -> None:
        body = _TrackingStream([b"{" + b"x" * 50, b"still unread"])

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                stream=body,
                headers={"content-type": "application/json", "content-length": "100"},
            )

        with _client(handler, max_bytes=10) as client, pytest.raises(SnapshotLimitError):
            client.list_records(
                _descriptor(),
                expected_identity_semantics=IDENTITY_SEMANTICS,
            )
        assert body.read_count == 0

    def test_nonfinite_numbers_and_duplicate_json_keys_fail(self) -> None:
        responses = [
            b'{"record_count":Infinity}',
            b'{"record_count":0,"record_count":0}',
        ]

        for response_body in responses:

            def handler(_: httpx.Request, content: bytes = response_body) -> httpx.Response:
                return httpx.Response(
                    200,
                    content=content,
                    headers={"content-type": "application/json"},
                )

            with _client(handler) as client, pytest.raises(SnapshotProtocolError, match="JSON"):
                client.list_records(
                    _descriptor(),
                    expected_identity_semantics=IDENTITY_SEMANTICS,
                )


def test_real_fastapi_sqlite_delta_snapshots_preserve_origin_publications(tmp_path: Any) -> None:
    """Read both sides of a delta through the real router and SQLite store."""
    from datetime import date, datetime, timezone

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from opendata.api.published_access import DatasetGrant, DatasetGrantRegistry
    from opendata.api.published_data import (
        get_dataset_contracts,
        get_dataset_grants,
        get_publication_store,
        router,
    )
    from opendata.data.dataset_contract_registry import DatasetContractRegistry, canonical_sha256
    from opendata.data.platform_contracts import DatasetContract, FieldContract, SourceIdentity
    from opendata.pipeline.publication_store import (
        PUBLICATION_METADATA,
        PublicationStore,
        PublishedRecordInput,
    )
    from sqlalchemy import create_engine

    from opendata.api.dependencies import Principal, get_current_principal
    from opendata.models.user import User, UserRole

    dataset_id = "equity.daily"
    source_id = "provider.alpha"
    start = date(2026, 10, 21)
    end = date(2026, 10, 23)
    utc = timezone.utc
    engine = create_engine(f"sqlite:///{tmp_path / 'sdk-published-api.db'}")
    PUBLICATION_METADATA.create_all(engine)
    store = PublicationStore(engine, cursor_signing_key=b"sdk-test-cursor-signing-key-32-bytes!")
    contract = DatasetContract(
        dataset_id=dataset_id,
        schema_version="1",
        semantic_version="1.0.0",
        asset_class="equity",
        frequency="1d",
        record_kind="time_series",
        fields=(
            FieldContract(name="instrument_id", data_type="string"),
            FieldContract(name="trade_date", data_type="date"),
            FieldContract(name="close", data_type="number"),
        ),
        business_key=("instrument_id", "trade_date"),
        ordering=("trade_date", "instrument_id"),
        temporal_semantics="business_date",
        time_field="trade_date",
        source=SourceIdentity(source_id=source_id, source="fixture-provider"),
        field_units={"close": "CNY/share"},
        field_currencies={"close": "CNY"},
        currency="CNY",
        adjustment_basis="raw",
        field_adjustments={"close": "raw"},
        timezone="Asia/Shanghai",
        pit_policy="unsupported",
    )
    contract_hash = canonical_sha256(contract)

    def record(instrument_id: str, trade_date: date, close: float) -> PublishedRecordInput:
        key = json.dumps(
            (instrument_id, trade_date.isoformat()), ensure_ascii=False, separators=(",", ":")
        )
        return PublishedRecordInput(
            business_key=key,
            business_date=trade_date,
            event_at=datetime.combine(trade_date, datetime.min.time(), tzinfo=utc),
            observed_at=datetime(2026, 10, 24, 1, tzinfo=utc),
            source_published_at=datetime(2026, 10, 24, 2, tzinfo=utc),
            available_at=datetime(2026, 10, 24, 3, tzinfo=utc),
            payload={
                "instrument_id": instrument_id,
                "trade_date": trade_date.isoformat(),
                "close": close,
            },
        )

    store.publish(
        dataset_id=dataset_id,
        source_id=source_id,
        schema_version=contract.schema_version,
        semantic_version=contract.semantic_version,
        unit="native",
        adjustment_basis="raw",
        quality={"status": "passed", "report_id": "DQ-SDK-1", "issue_count": 0},
        provenance={"contract_sha256": contract_hash},
        idempotency_key="sdk-snapshot-publication-initial",
        records=(record("AAA", start, 100.0), record("BBB", start, 200.0)),
    )
    grants = DatasetGrantRegistry(
        (
            DatasetGrant(
                dataset_id=dataset_id,
                domain="equity",
                source_id=source_id,
                permitted_operations=(
                    "data.read",
                    "snapshot.create",
                    "snapshot.read",
                ),
                purposes=("research",),
                valid_from=datetime(2026, 1, 1, tzinfo=utc),
                valid_until=datetime(2027, 1, 1, tzinfo=utc),
                review_evidence="OD204-SDK",
            ),
        )
    )
    contracts = DatasetContractRegistry((contract,))
    user = User(
        id=7,
        username="sdk-user",
        email="sdk-user@example.test",
        hashed_password="test-only-not-a-credential",
        role=UserRole.USER,
        is_active=True,
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_principal] = lambda: Principal(user=user)
    app.dependency_overrides[get_dataset_grants] = lambda: grants
    app.dependency_overrides[get_dataset_contracts] = lambda: contracts
    app.dependency_overrides[get_publication_store] = lambda: store

    expected_semantics: dict[str, Any] = {
        "version": 1,
        "identity_key_encoding": "canonical-json-array-v1",
        "revision": (
            "latest non-deleted revision per business_key at the pinned publication sequence"
        ),
        "temporal_fields": {
            "business_date": (
                "business-date bucket; null ordering last (route rejects null time values)"
            ),
            "observed_at": "source observation time",
            "source_published_at": "source publication time",
            "available_at": "platform availability time",
            "event_at": "source event time",
        },
        "business_key_fields": ["instrument_id", "trade_date"],
        "ordering_fields": ["trade_date", "instrument_id"],
        "storage_order": [
            {
                "field": "business_date",
                "direction": "asc",
                "nulls": "last",
                "comparison": "native",
            },
            {
                "field": "business_key",
                "direction": "asc",
                "comparison": "binary_lexicographic",
            },
        ],
        "temporal_semantics": "business_date",
        "time_field": "trade_date",
    }

    requests: list[tuple[str, str]] = []
    with TestClient(app) as http_client:

        def observe_request(request: httpx.Request) -> None:
            requests.append((request.method, request.url.path))

        http_client.event_hooks["request"].append(observe_request)
        with SnapshotClient(
            "http://test",
            api_key="sdk-integration-test",
            http_client=http_client,
        ) as client:
            old_snapshot = client.create_snapshot(
                dataset_id,
                start_date=start.isoformat(),
                end_date=end.isoformat(),
                page_size=1,
                expected_identity_semantics=expected_semantics,
            )
            old_publication_id = old_snapshot.publication_id
            store.publish(
                dataset_id=dataset_id,
                source_id=source_id,
                schema_version=contract.schema_version,
                semantic_version=contract.semantic_version,
                unit="native",
                adjustment_basis="raw",
                quality={"status": "passed", "report_id": "DQ-SDK-DELTA", "issue_count": 0},
                provenance={"contract_sha256": contract_hash},
                idempotency_key="sdk-snapshot-publication-delta",
                records=(record("BBB", start, 201.0),),
            )
            latest_snapshot = client.create_snapshot(
                dataset_id,
                start_date=start.isoformat(),
                end_date=end.isoformat(),
                page_size=1,
                expected_identity_semantics=expected_semantics,
            )
            latest_rows = list(
                client.iter_records(
                    latest_snapshot,
                    expected_identity_semantics=expected_semantics,
                )
            )
            old_rows = list(
                client.iter_records(
                    old_snapshot,
                    expected_identity_semantics=expected_semantics,
                )
            )

    assert old_snapshot.record_count == latest_snapshot.record_count == 2
    assert [item["payload"]["close"] for item in latest_rows] == [100.0, 201.0]
    assert [item["payload"]["instrument_id"] for item in latest_rows] == ["AAA", "BBB"]
    assert [item["publication_sequence"] for item in latest_rows] == [1, 2]
    assert [item["publication_id"] for item in latest_rows] == [
        old_publication_id,
        latest_snapshot.publication_id,
    ]
    assert [item["payload"]["close"] for item in old_rows] == [100.0, 200.0]
    assert [item["publication_sequence"] for item in old_rows] == [1, 1]
    assert [item["publication_id"] for item in old_rows] == [old_publication_id] * 2
    assert requests == [
        ("POST", "/api/v2/snapshots"),
        ("POST", "/api/v2/snapshots"),
        ("GET", "/api/v2/datasets/equity.daily/records"),
        ("GET", "/api/v2/datasets/equity.daily/records"),
        ("GET", "/api/v2/datasets/equity.daily/records"),
        ("GET", "/api/v2/datasets/equity.daily/records"),
    ]
