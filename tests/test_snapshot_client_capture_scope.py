"""Behavior tests for typed instrument/capture snapshot scope binding."""

from __future__ import annotations

import copy
import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from opendata_client.snapshots import (
    InstrumentCaptureSelection,
    SnapshotClient,
    SnapshotProtocolError,
    SnapshotScopeError,
)

if TYPE_CHECKING:
    from collections.abc import Callable


INSTRUMENT_ID = "600519"
CAPTURE_ID = "a" * 64
OTHER_CAPTURE_ID = "b" * 64
DATASET_ID = "sina_fzb_report_observations"
CAPTURE_FILTER_FIELDS = ["instrument_id", "capture_id"]
CAPTURE_IDENTITY_SEMANTICS: dict[str, Any] = {
    "version": 1,
    "business_key_fields": ["instrument_id", "capture_id", "statement_code", "report_period"],
    "identity_key_encoding": "canonical-json-array-v1",
    "revision": "latest non-deleted revision per business_key at the pinned publication sequence",
    "ordering_fields": ["instrument_id", "capture_id", "statement_code", "report_period"],
    "storage_order": [
        {
            "field": "business_date",
            "direction": "asc",
            "nulls": "last",
            "comparison": "native",
        },
        {"field": "business_key", "direction": "asc", "comparison": "binary_lexicographic"},
    ],
    "temporal_semantics": "none",
    "time_field": None,
    "temporal_fields": {
        "business_date": "null for atemporal table records; not a historical business date",
        "observed_at": "source observation time",
        "source_published_at": "source publication time",
        "available_at": "platform availability time",
        "event_at": "source event time",
    },
}


def _capture_snapshot(**changes: Any) -> dict[str, Any]:
    """Return a bounded synthetic selected snapshot descriptor."""
    data: dict[str, Any] = {
        "snapshot_id": "b" * 32,
        "dataset_id": DATASET_ID,
        "publication_id": "c" * 32,
        "sequence": 17,
        "schema_version": "source-native-report-v1",
        "semantic_version": "sina-fzb-source-native-v1",
        "source": {"source_id": "sina-fzb", "source": "sina"},
        "schema": [
            {"name": "instrument_id", "data_type": "string", "nullable": False},
            {"name": "capture_id", "data_type": "string", "nullable": False},
            {"name": "statement_code", "data_type": "string", "nullable": False},
            {"name": "report_period", "data_type": "date", "nullable": False},
        ],
        "quality": {"status": "unknown", "report_id": None, "issue_count": 0},
        "field_units": {},
        "field_currencies": {},
        "field_adjustments": {},
        "currency": None,
        "adjustment_basis": "not_applicable",
        "asset_class": "equity",
        "frequency": "irregular",
        "record_kind": "table",
        "business_key_fields": ["instrument_id", "capture_id", "statement_code", "report_period"],
        "ordering_fields": ["instrument_id", "capture_id", "statement_code", "report_period"],
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
        "filter_fields": copy.deepcopy(CAPTURE_FILTER_FIELDS),
        "time_field": None,
        "temporal_semantics": "none",
        "timezone": "UTC",
        "pit_policy": "unsupported",
        "source_available_at_field": None,
        "calendar_id": None,
        "contract_sha256": "d" * 64,
        "window": {"start": None, "end": None, "end_exclusive": True},
        "page_size": 1,
        "as_of": "2024-02-02T00:00:00Z",
        "created_at": "2024-02-02T00:00:01Z",
        "expires_at": "2024-02-03T00:00:01Z",
        "record_count": 2,
        "selection_sha256": "e" * 64,
        "content_sha256": "f" * 64,
        "identity_semantics": copy.deepcopy(CAPTURE_IDENTITY_SEMANTICS),
        "selection": {"instrument_id": INSTRUMENT_ID, "capture_id": CAPTURE_ID},
    }
    data.update(changes)
    return data


def _capture_row(
    period: str,
    *,
    instrument_id: str = INSTRUMENT_ID,
    capture_id: str = CAPTURE_ID,
) -> dict[str, Any]:
    """Return a synthetic atemporal report row with a matching canonical key."""
    key_values = [instrument_id, capture_id, "fzb", period]
    return {
        "dataset_id": DATASET_ID,
        "business_key": json.dumps(key_values, ensure_ascii=False, separators=(",", ":")),
        "revision": 1,
        "publication_id": "c" * 32,
        "publication_sequence": 17,
        "business_date": None,
        "event_at": None,
        "observed_at": "2024-02-02T00:00:00Z",
        "source_published_at": None,
        "available_at": None,
        "payload": {
            "instrument_id": instrument_id,
            "capture_id": capture_id,
            "statement_code": "fzb",
            "report_period": period,
        },
        "record_hash": "1" * 64,
    }


def _capture_page(
    rows: list[dict[str, Any]],
    *,
    snapshot: dict[str, Any] | None = None,
    meta_snapshot: dict[str, Any] | None = None,
    next_cursor: str | None = None,
    has_more: bool = False,
) -> dict[str, Any]:
    """Return one synthetic page with separately controllable pinned metadata."""
    pinned = _capture_snapshot() if snapshot is None else snapshot
    nested = pinned if meta_snapshot is None else meta_snapshot
    return {
        **copy.deepcopy(pinned),
        "rows": rows,
        "count": len(rows),
        "next_cursor": next_cursor,
        "has_more": has_more,
        "meta": {
            "snapshot": copy.deepcopy(nested),
            "ordering_fields": copy.deepcopy(pinned["ordering_fields"]),
            "storage_order": copy.deepcopy(pinned["storage_order"]),
            "identity_key_encoding": pinned["identity_key_encoding"],
        },
    }


def _legacy_snapshot(**changes: Any) -> dict[str, Any]:
    """Return the established dated time-series snapshot response vector."""
    identity = {
        "version": 1,
        "business_key_fields": ["symbol"],
        "identity_key_encoding": "canonical-json-array-v1",
        "revision": (
            "latest non-deleted revision per business_key at the pinned publication sequence"
        ),
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
    data = {
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
        ],
        "quality": {"status": "passed", "report_id": "quality-001", "issue_count": 0},
        "field_units": {},
        "field_currencies": {},
        "field_adjustments": {},
        "currency": "CNY",
        "adjustment_basis": "raw",
        "asset_class": "equity",
        "frequency": "1d",
        "record_kind": "time_series",
        "business_key_fields": ["symbol"],
        "ordering_fields": ["business_date", "symbol"],
        "storage_order": copy.deepcopy(identity["storage_order"]),
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
        "identity_semantics": identity,
    }
    data.update(changes)
    return data


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> SnapshotClient:
    """Build a snapshot client backed only by an in-memory MockTransport."""
    return SnapshotClient(
        "https://data.example",
        api_key="od-test-key",
        transport=httpx.MockTransport(handler),
    )


class TestInstrumentCaptureSnapshotClient:
    """Exercise selected snapshot request and response scope binding."""

    def test_create_posts_only_the_pair_and_accepts_an_unbounded_window(self) -> None:
        seen: list[bytes] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.read())
            return httpx.Response(201, json=_capture_snapshot())

        with _client(handler) as client:
            snapshot = client.create_snapshot(
                DATASET_ID,
                expected_identity_semantics=CAPTURE_IDENTITY_SEMANTICS,
                page_size=1,
                instrument_id=INSTRUMENT_ID,
                capture_id=CAPTURE_ID,
            )

        assert seen == [
            (
                b'{"dataset_id":"sina_fzb_report_observations","page_size":1,'
                b'"instrument_id":"600519","capture_id":"' + CAPTURE_ID.encode() + b'"}'
            )
        ]
        assert snapshot.selection == InstrumentCaptureSelection(INSTRUMENT_ID, CAPTURE_ID)
        assert snapshot.window.start is None
        assert snapshot.window.end is None

    def test_selected_empty_snapshot_is_accepted(self) -> None:
        with _client(
            lambda _: httpx.Response(201, json=_capture_snapshot(record_count=0))
        ) as client:
            snapshot = client.create_snapshot(
                DATASET_ID,
                expected_identity_semantics=CAPTURE_IDENTITY_SEMANTICS,
                instrument_id=INSTRUMENT_ID,
                capture_id=CAPTURE_ID,
            )

        assert snapshot.record_count == 0
        assert snapshot.selection == InstrumentCaptureSelection(INSTRUMENT_ID, CAPTURE_ID)

    def test_iteration_keeps_the_snapshot_selection_fixed_across_pages(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.method == "POST":
                return httpx.Response(201, json=_capture_snapshot(page_size=1))
            if request.url.params.get("cursor") is None:
                return httpx.Response(
                    200,
                    json=_capture_page(
                        [_capture_row("2023-12-31")],
                        snapshot=_capture_snapshot(page_size=1),
                        next_cursor="opaque-cursor-1",
                        has_more=True,
                    ),
                )
            return httpx.Response(
                200,
                json=_capture_page(
                    [_capture_row("2024-12-31")],
                    snapshot=_capture_snapshot(page_size=1),
                ),
            )

        with _client(handler) as client:
            snapshot = client.create_snapshot(
                DATASET_ID,
                expected_identity_semantics=CAPTURE_IDENTITY_SEMANTICS,
                page_size=1,
                instrument_id=INSTRUMENT_ID,
                capture_id=CAPTURE_ID,
            )
            rows = list(
                client.iter_records(
                    snapshot,
                    expected_identity_semantics=CAPTURE_IDENTITY_SEMANTICS,
                )
            )

        assert len(rows) == 2
        assert [request.url.path for request in requests] == [
            "/api/v2/snapshots",
            "/api/v2/datasets/sina_fzb_report_observations/records",
            "/api/v2/datasets/sina_fzb_report_observations/records",
        ]
        assert dict(requests[1].url.params) == {"snapshot_id": "b" * 32}
        assert dict(requests[2].url.params) == {
            "snapshot_id": "b" * 32,
            "cursor": "opaque-cursor-1",
        }

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"instrument_id": INSTRUMENT_ID}, "supplied together"),
            ({"capture_id": CAPTURE_ID}, "supplied together"),
            ({"instrument_id": INSTRUMENT_ID, "capture_id": "A" * 64}, "selection is invalid"),
            (
                {
                    "instrument_id": INSTRUMENT_ID,
                    "capture_id": CAPTURE_ID,
                    "start_date": "2024-01-01",
                    "end_date": "2025-01-01",
                },
                "do not accept date windows",
            ),
            ({}, "require start_date and end_date"),
            ({"start_date": "2024-01-01"}, "supplied together"),
            (
                {"start_date": "2024-01-01", "end_date": "2024-02-01"},
                "atemporal identity semantics require instrument/capture selection",
            ),
        ],
    )
    def test_invalid_selector_and_date_combinations_fail_before_transport(
        self, kwargs: dict[str, Any], message: str
    ) -> None:
        calls = 0

        def handler(_: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(201, json=_capture_snapshot())

        with _client(handler) as client, pytest.raises(ValueError, match=message):
            client.create_snapshot(
                DATASET_ID,
                expected_identity_semantics=CAPTURE_IDENTITY_SEMANTICS,
                **kwargs,
            )
        assert calls == 0

    def test_selected_request_rejects_response_with_another_pair(self) -> None:
        response = _capture_snapshot(
            selection={"instrument_id": INSTRUMENT_ID, "capture_id": OTHER_CAPTURE_ID}
        )

        with (
            _client(lambda _: httpx.Response(201, json=response)) as client,
            pytest.raises(SnapshotScopeError, match="requested scope"),
        ):
            client.create_snapshot(
                DATASET_ID,
                expected_identity_semantics=CAPTURE_IDENTITY_SEMANTICS,
                instrument_id=INSTRUMENT_ID,
                capture_id=CAPTURE_ID,
            )

    @pytest.mark.parametrize(
        "changes",
        [
            {"selection": None},
            {"filter_fields": ["capture_id", "instrument_id"]},
            {"record_kind": "time_series"},
            {"temporal_semantics": "business_date"},
            {"pit_policy": "supported"},
            {"business_key_fields": ["capture_id", "statement_code", "report_period"]},
            {
                "schema": [
                    {"name": "instrument_id", "data_type": "string", "nullable": False},
                    {"name": "capture_id", "data_type": "string", "nullable": True},
                    {"name": "statement_code", "data_type": "string", "nullable": False},
                    {"name": "report_period", "data_type": "date", "nullable": False},
                ]
            },
            {"window": {"start": "2024-01-01", "end": None, "end_exclusive": True}},
        ],
    )
    def test_reserved_profile_and_selection_metadata_fail_closed(
        self, changes: dict[str, Any]
    ) -> None:
        with (
            _client(lambda _: httpx.Response(201, json=_capture_snapshot(**changes))) as client,
            pytest.raises(SnapshotProtocolError),
        ):
            client.create_snapshot(
                DATASET_ID,
                expected_identity_semantics=CAPTURE_IDENTITY_SEMANTICS,
                instrument_id=INSTRUMENT_ID,
                capture_id=CAPTURE_ID,
            )

    def test_unreserved_profile_rejects_selection_metadata(self) -> None:
        legacy = _legacy_snapshot(
            selection={"instrument_id": INSTRUMENT_ID, "capture_id": CAPTURE_ID}
        )
        with (
            _client(lambda _: httpx.Response(201, json=legacy)) as client,
            pytest.raises(SnapshotProtocolError, match="unselected snapshot"),
        ):
            client.create_snapshot(
                "stock_daily",
                start_date="2024-01-01",
                end_date="2024-02-01",
                expected_identity_semantics=legacy["identity_semantics"],
            )

    def test_empty_atemporal_snapshot_cannot_claim_a_dated_non_capture_scope(self) -> None:
        unselected = _capture_snapshot(
            filter_fields=[],
            record_count=0,
            window={"start": "2024-01-01", "end": "2024-02-01", "end_exclusive": True},
        )
        unselected.pop("selection")

        with (
            _client(lambda _: httpx.Response(200, json=unselected)) as client,
            pytest.raises(
                SnapshotProtocolError,
                match="atemporal snapshots require the instrument/capture profile",
            ),
        ):
            client.get_snapshot(
                unselected["snapshot_id"],
                dataset_id=DATASET_ID,
                expected_identity_semantics=CAPTURE_IDENTITY_SEMANTICS,
            )

    def test_page_rejects_changed_selection_in_metadata_or_row(self) -> None:
        pinned = _capture_snapshot(record_count=1, page_size=1)
        mismatched_meta = _capture_snapshot(
            record_count=1,
            page_size=1,
            selection={"instrument_id": INSTRUMENT_ID, "capture_id": OTHER_CAPTURE_ID},
        )
        bad_pages = [
            _capture_page(
                [_capture_row("2024-12-31")],
                snapshot=_capture_snapshot(
                    record_count=1,
                    page_size=1,
                    selection={"instrument_id": INSTRUMENT_ID, "capture_id": OTHER_CAPTURE_ID},
                ),
            ),
            _capture_page(
                [_capture_row("2024-12-31")],
                snapshot=pinned,
                meta_snapshot=mismatched_meta,
            ),
            _capture_page(
                [_capture_row("2024-12-31", capture_id=OTHER_CAPTURE_ID)],
                snapshot=pinned,
            ),
            _capture_page(
                [_capture_row("2024-12-31", instrument_id="600520")],
                snapshot=pinned,
            ),
        ]

        for page in bad_pages:
            with (
                _client(lambda _, body=page: httpx.Response(200, json=body)) as client,
                pytest.raises((SnapshotScopeError, SnapshotProtocolError)),
            ):
                client.list_records(
                    _snapshot_descriptor(pinned),
                    expected_identity_semantics=CAPTURE_IDENTITY_SEMANTICS,
                )

    def test_selected_rows_must_have_null_business_date(self) -> None:
        row = _capture_row("2024-12-31")
        row["business_date"] = "2024-12-31"
        page = _capture_page([row], snapshot=_capture_snapshot(record_count=1, page_size=1))

        with (
            _client(lambda _: httpx.Response(200, json=page)) as client,
            pytest.raises(SnapshotProtocolError, match="business_date must be null"),
        ):
            client.list_records(
                _snapshot_descriptor(_capture_snapshot(record_count=1, page_size=1)),
                expected_identity_semantics=CAPTURE_IDENTITY_SEMANTICS,
            )

    def test_legacy_snapshot_request_bytes_and_dated_window_are_unchanged(self) -> None:
        seen: list[bytes] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.read())
            return httpx.Response(201, json=_legacy_snapshot())

        with _client(handler) as client:
            snapshot = client.create_snapshot(
                "stock_daily",
                start_date="2024-01-01",
                end_date="2024-02-01",
                page_size=2,
                publication_id="c" * 32,
                expected_identity_semantics=_legacy_snapshot()["identity_semantics"],
            )

        assert seen == [
            b'{"dataset_id":"stock_daily","start_date":"2024-01-01",'
            b'"end_date":"2024-02-01","page_size":2,"publication_id":"' + b"c" * 32 + b'"}'
        ]
        assert snapshot.selection is None
        assert snapshot.window.start is not None
        assert snapshot.window.start.isoformat() == "2024-01-01"
        assert snapshot.window.end is not None
        assert snapshot.window.end.isoformat() == "2024-02-01"


def _snapshot_descriptor(payload: dict[str, Any]):
    """Parse a synthetic descriptor through the same response path used by the client."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=payload)

    with _client(handler) as client:
        return client.get_snapshot(
            payload["snapshot_id"],
            dataset_id=payload["dataset_id"],
            expected_identity_semantics=payload["identity_semantics"],
        )
